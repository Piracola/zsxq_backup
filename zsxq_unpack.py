#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zsxq_unpack.py —— 知识星球(通付盾 WSSEC/VMP 加固)一键静默脱壳工具

原理(无需 frida / 无需刷模块 / 无需改 ROM):
  加固壳在运行期把解密后的 DEX 放在内存匿名映射里。本工具以 root 身份
  直接读 /proc/<pid>/mem(不 ptrace、不注入、不加载任何 so),
  因此 TracerPid 保持为 0,壳的反调试全程无感,也不会像 frida-server
  那样在 HyperOS 上把 zygote/zn-daemon 的进程创建管道搞挂。

  然后:扫描内存镜像里的 DEX 魔数 -> 按 dex 头 file_size 跨 VMA 拼接
  -> 重算 SHA-1/Adler32 修复被 ART quickening 改坏的头部 -> 输出完整 DEX。

用法:
  python zsxq_unpack.py                        自动启动已安装的知识星球并脱壳
  python zsxq_unpack.py --apk zsxq5.40.apk     先安装指定 APK 再脱壳
  python zsxq_unpack.py --no-jadx              只出 DEX,不反编译
  python zsxq_unpack.py --manual               启动后手动确认已到主页再回车
  python zsxq_unpack.py --keep-raw             保留内存原始镜像(默认提取成功后删除)
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import time
import zlib
from datetime import datetime

DEX_MAGICS = [bytes.fromhex(h) for h in (
    '6465780a30333500', '6465780a30333600', '6465780a30333700',
    '6465780a30333800', '6465780a30333900')]
CDEX_MAGIC = bytes.fromhex('6364657830303100')
PAGE = 4096
ACC_NATIVE = 0x100
ACC_ABSTRACT = 0x400
MAX_DEX = 128 * 1024 * 1024
MIN_DEX = 1024        # 小于 1KB 的"DEX"是误报

SKIP_PARTS = ('.ttf', '.ttc', '.otf', '.dat', '.so', '.jar', '.art', '.oat',
              '.vdex', '.odex', '.mdb', '.apk', '.zip', '.db', '.json', '.png',
              '.jpg', '.webp', '.pak', '.prof', '.xml', '.cnt', '.dex')
SKIP_PREFIX = ('/system', '/apex', '/vendor', '/product', '/system_ext',
               '/data/dalvik-cache', '/dev/', '/data/misc')
SKIP_CONTAINS = ('jit-cache', 'jit-zygote', 'dalvik-free list', 'dalvik-main space',
                 'dalvik-region space', 'dalvik-rb copying', 'dalvik-concurrent copying',
                 'dalvik-thread local mark', 'dalvik-live stack', 'dalvik-allocation stack',
                 'dalvik-card table', 'dalvik-CompilerMetadata', 'dalvik-zygote space',
                 'dalvik-RegionSpace', 'thread signal stack', 'stack_and_tls')

ADB = None
SERIAL = None


def log(msg):
    print(msg, flush=True)


def die(msg, code=1):
    log('❌ ' + msg)
    sys.exit(code)


def run(cmd, timeout=300):
    return subprocess.run(cmd, capture_output=True, encoding='utf-8',
                          errors='replace', timeout=timeout)


def adb(*args, timeout=300):
    base = [ADB]
    if SERIAL:
        base += ['-s', SERIAL]
    return run(base + list(args), timeout=timeout)


def shell(cmd, timeout=300):
    return adb('shell', cmd, timeout=timeout)


def su(cmd, timeout=600):
    return adb('shell', 'su -c "%s"' % cmd, timeout=timeout)


def su_out(cmd, timeout=600):
    return (su(cmd, timeout=timeout).stdout or '').strip()


def parse_maps(text):
    out = []
    for line in text.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 2:
            continue
        rng = parts[0]
        if '-' not in rng:
            continue
        a, b = rng.split('-', 1)
        try:
            start = int(a, 16)
            end = int(b, 16)
        except ValueError:
            continue
        out.append((start, end, parts[1], parts[5].strip() if len(parts) > 5 else ''))
    out.sort()
    return out


def vma_end(vmas, cur, limit):
    for s, e, p, n in vmas:
        if s <= cur < e:
            if p.startswith('---'):
                break
            return min(e, limit)
    return min(limit, cur + PAGE)


def score_region(name, pkg):
    if 'dalvik-DEX data' in name:
        return 100
    if 'dalvik-classes.dex' in name:
        return 92
    if name == '':
        return 90
    if 'libc_malloc' in name:
        return 85
    if name.startswith('/memfd:'):
        return 80
    if 'ashmem' in name:
        return 70
    if '.bss' in name:
        return 60
    if 'dalvik-LinearAlloc' in name:
        return 55
    if 'dalvik-non moving space' in name:
        return 50
    if 'linker_alloc' in name:
        return 45
    if pkg in name:
        return 40
    return 10


def pick_candidates(vmas, pkg, cap_mb, max_total_mb):
    cands = []
    for s, e, p, n in vmas:
        size = e - s
        if size < PAGE or size > cap_mb * 1024 * 1024:
            continue
        if p.startswith('---'):
            continue
        low = n.lower()
        if any(low.endswith(x) for x in SKIP_PARTS):
            continue
        if any(n.startswith(x) for x in SKIP_PREFIX):
            continue
        if any(x in n for x in SKIP_CONTAINS):
            continue
        cands.append((score_region(n, pkg), s, e, p, n, size))
    cands.sort(key=lambda c: (-c[0], -c[5]))
    keep = []
    total = 0
    limit = max_total_mb * 1024 * 1024
    for c in cands:
        if total + c[5] > limit:
            continue
        keep.append(c)
        total += c[5]
    return keep, total


def dd_line(pid, start, size, dest, name):
    skip = start // PAGE
    count = (size + PAGE - 1) // PAGE
    return 'dd if=/proc/%d/mem of=%s/%s bs=4096 skip=%d count=%d 2>/dev/null' % (
        pid, dest, name, skip, count)


def build_script(lines, dest, jobs):
    body = ['#!/system/bin/sh', 'mkdir -p ' + dest, 'rm -f ' + dest + '/*']
    groups = [[] for _ in range(max(1, jobs))]
    for i, ln in enumerate(lines):
        groups[i % len(groups)].append(ln)
    for g in groups:
        if not g:
            continue
        body.append('(')
        body.extend(g)
        body.append(') &')
    body.append('wait')
    body.append('echo UNPACKDONE')
    return chr(10).join(body) + chr(10)


def push_and_run(script_text, workdir, lines_count, label):
    local = os.path.join(workdir, 'dev_script_%s.sh' % label)
    with open(local, 'w', newline=chr(10), encoding='utf-8') as f:
        f.write(script_text)
    remote = '/data/local/tmp/zsxq_unpack_%s.sh' % label
    r = adb('push', local, remote)
    if r.returncode != 0:
        die('推送脚本失败: ' + r.stderr)
    out = su('sh ' + remote)
    if 'UNPACKDONE' not in (out.stdout or ''):
        log('⚠ 设备脚本输出异常: ' + (out.stdout or '')[-300:] + (out.stderr or '')[-300:])
    return out


def index_dumps(root):
    idx = []
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if not fn.endswith('.bin'):
                continue
            stem = fn[:-4]
            if '_' not in stem:
                continue
            a, b = stem.split('_')[:2]
            try:
                start = int(a, 16)
                size = int(b, 16)
            except ValueError:
                continue
            idx.append((start, start + size, os.path.join(dirpath, fn)))
    idx.sort()
    return idx


def covered(idx, start, end):
    for s, e, p in idx:
        if s <= start and e >= end:
            return True
    return False


def find_cover(idx, start, end):
    best = None
    for s, e, p in idx:
        if s <= start and e >= end:
            if best is None or (e - s) < (best[1] - best[0]):
                best = (s, e, p)
    return best


def scan_for_dex(idx, verbose=True):
    found = []
    seen = set()
    for s, e, path in idx:
        try:
            data = open(path, 'rb').read()
        except OSError:
            continue
        for mg in DEX_MAGICS:
            pos = 0
            while True:
                i = data.find(mg, pos)
                if i < 0:
                    break
                pos = i + 1
                if i + 112 > len(data):
                    continue
                file_size, header_size, endian = struct.unpack_from('<III', data, i + 32)
                if endian != 0x12345678 or header_size != 0x70:
                    continue
                if not (MIN_DEX <= file_size <= MAX_DEX):
                    continue
                str_ids = struct.unpack_from('<I', data, i + 56)[0]
                map_off = struct.unpack_from('<I', data, i + 52)[0]
                if str_ids == 0 or map_off == 0 or map_off % 4 != 0:
                    continue
                abs_addr = s + i
                if abs_addr in seen:
                    continue
                seen.add(abs_addr)
                found.append({'abs': abs_addr, 'size': file_size,
                              'ver': mg[4:7].decode('latin1')})
    found.sort(key=lambda d: -d['size'])
    if verbose:
        log('  发现 DEX 头 %d 个' % len(found))
    return found


def assemble(idx, vmas, start, size):
    buf = bytearray()
    cur = start
    limit = start + size
    gaps = 0
    while cur < limit:
        ve = vma_end(vmas, cur, limit)
        if ve <= cur:
            ve = min(limit, cur + PAGE)
        cover = find_cover(idx, cur, ve)
        if cover is None:
            buf += bytes(ve - cur)
            gaps += 1
        else:
            data = open(cover[2], 'rb').read()
            off = cur - cover[0]
            buf += data[off:off + (ve - cur)]
        cur = ve
    return bytes(buf[:size]), gaps


def uleb(b, o):
    r = 0
    s = 0
    while True:
        x = b[o]
        o += 1
        r |= (x & 0x7f) << s
        if not (x & 0x80):
            break
        s += 7
    return r, o


def fix_and_validate(blob):
    b = bytearray(blob)
    file_size, header_size, endian = struct.unpack_from('<III', b, 32)
    if bytes(b[0:4]) != b'dex' + bytes([10]) or endian != 0x12345678:
        return None, 'not-dex'
    if file_size != len(b):
        return None, 'size-mismatch'
    sig = hashlib.sha1(bytes(b[32:])).digest()
    b[12:32] = sig
    struct.pack_into('<I', b, 8, zlib.adler32(bytes(b[12:])) & 0xffffffff)
    u32 = lambda o: struct.unpack_from('<I', b, o)[0]
    sids, sidso = u32(56), u32(60)
    tids, tidso = u32(64), u32(68)
    pids, pidso = u32(72), u32(76)
    fids, fidso = u32(80), u32(84)
    mids, midso = u32(88), u32(92)
    cds, cdso = u32(96), u32(100)
    map_off = u32(52)

    def ok(o, cnt, esz):
        return o == 0 or (o % 4 == 0 and o + cnt * esz <= len(b))

    struct_ok = (ok(sidso, sids, 4) and ok(tidso, tids, 4) and ok(pidso, pids, 12) and
                 ok(fidso, fids, 8) and ok(midso, mids, 8) and ok(cdso, cds, 32) and
                 (map_off == 0 or (map_off % 4 == 0 and map_off < len(b))))
    if not struct_ok or sids == 0:
        return None, 'bad-structure'
    return bytes(b), 'ok'


def dex_stats(blob):
    u32 = lambda o: struct.unpack_from('<I', blob, o)[0]
    cds, cdo = u32(96), u32(100)
    stro = u32(60)
    tidso = u32(68)
    tot = wc = nat = abst = 0

    def S(i):
        o = u32(stro + i * 4)
        n, o = uleb(blob, o)
        return blob[o:o + n].decode('utf-8', 'replace')

    def T(ti):
        return S(struct.unpack_from('<I', blob, tidso + ti * 4)[0])

    for i in range(cds):
        o = cdo + i * 32
        cdata = u32(o + 24)
        if not cdata:
            continue
        p = cdata
        sf, p = uleb(blob, p)
        inf, p = uleb(blob, p)
        dm, p = uleb(blob, p)
        vm, p = uleb(blob, p)
        for _ in range(sf + inf):
            _, p = uleb(blob, p)
            _, p = uleb(blob, p)
        for _ in range(dm + vm):
            _, p = uleb(blob, p)
            acc, p = uleb(blob, p)
            coff, p = uleb(blob, p)
            tot += 1
            if coff:
                wc += 1
            elif acc & ACC_NATIVE:
                nat += 1
            elif acc & ACC_ABSTRACT:
                abst += 1
    return {'classes': cds, 'methods': tot, 'with_code': wc,
            'native': nat, 'abstract': abst, 'unexplained': tot - wc - nat - abst}


def resolve_adb():
    global ADB, SERIAL
    ADB = shutil.which('adb') or 'adb'
    r = run([ADB, 'devices'])
    lines = [l for l in (r.stdout or '').splitlines()[1:] if l.strip()]
    devs = [l.split()[0] for l in lines if l.strip().endswith('device')]
    if not devs:
        die('未检测到已连接的设备（adb devices 为空）。请确认手机已连接并开启 USB 调试。')
    if len(devs) > 1:
        log('检测到多台设备: ' + ', '.join(devs))
        SERIAL = devs[0]
        log('使用第一台: ' + SERIAL)
    else:
        SERIAL = devs[0]
    log('✓ 设备: ' + SERIAL)


def check_root():
    r = su('id')
    out = (r.stdout or '')
    if 'uid=0' not in out:
        die('设备未获得 root 权限（su -c id 未返回 uid=0）。本工具需要 root。')
    log('✓ root 权限正常')
    return out.strip()


def find_jadx(explicit):
    if explicit:
        return explicit
    cand = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'work', 'jadx', 'bin',
                        'jadx.bat' if os.name == 'nt' else 'jadx')
    if os.path.exists(cand):
        return cand
    w = shutil.which('jadx') or shutil.which('jadx.bat')
    return w


def find_java_home():
    for env in ('JAVA_HOME',):
        v = os.environ.get(env)
        if v and os.path.exists(v):
            return v
    base = os.path.expanduser('~/.jdks')
    if os.path.isdir(base):
        for name in sorted(os.listdir(base), reverse=True):
            p = os.path.join(base, name)
            if os.path.isdir(p):
                return p
    return None


def is_installed(pkg):
    r = shell('pm list packages ' + pkg)
    return pkg in (r.stdout or '')


def install_apk(apk):
    log('安装 APK: %s' % os.path.basename(apk))
    r = adb('install', '-r', '-d', apk, timeout=1200)
    out = (r.stdout or '') + (r.stderr or '')
    if 'Success' not in out:
        log('首次安装失败，尝试卸载后重装...')
        log(out.strip()[-400:])
        adb('uninstall', PACKAGE, timeout=300)
        r = adb('install', '-r', '-d', apk, timeout=1200)
        out = (r.stdout or '') + (r.stderr or '')
        if 'Success' not in out:
            die('APK 安装失败: ' + out.strip()[-400:])
    log('✓ APK 安装成功')


def get_pid():
    out = su_out('pidof ' + PACKAGE)
    for tok in out.split():
        if tok.isdigit():
            return int(tok)
    return None


def force_stop():
    shell('am force-stop ' + PACKAGE)


def launch():
    r = shell('am start -n %s/%s' % (PACKAGE, MAIN_ACTIVITY))
    return 'Error' not in (r.stdout or '') and 'Exception' not in (r.stdout or '')


def wait_for_pid(timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        pid = get_pid()
        if pid:
            return pid
        time.sleep(1)
    return None


def extract(workdir, pid, pkg, args):
    raw_root = os.path.join(workdir, 'raw')
    dev_root = '/data/local/tmp/zsxq_raw'
    if os.path.isdir(raw_root):
        shutil.rmtree(raw_root, ignore_errors=True)
    os.makedirs(raw_root, exist_ok=True)

    log('')
    log('[2/5] 读取进程内存映射 (%d)' % pid)
    maps_txt = su_out('cat /proc/%d/maps' % pid)
    if not maps_txt:
        die('无法读取 /proc/%d/maps（进程可能已退出）' % pid)
    open(os.path.join(workdir, 'maps.txt'), 'w', encoding='utf-8').write(maps_txt)
    vmas = parse_maps(maps_txt)
    log('  VMA 条目: %d' % len(vmas))

    log('[3/5] 转储候选内存区域')
    cands, total = pick_candidates(vmas, pkg, args.max_region_mb, args.max_total_mb)
    log('  候选区域 %d 块，共 %.1f MB' % (len(cands), total / 1048576.0))
    lines = [dd_line(pid, s, sz, dev_root, '%016x_%08x.bin' % (s, sz))
             for _sc, s, _e, _p, _n, sz in cands]
    push_and_run(build_script(lines, dev_root, args.jobs), workdir, len(lines), 'raw')
    du = su_out('du -sm ' + dev_root)
    log('  设备端已转储: %s' % (du or '未知'))

    log('  拉取到本机...')
    r = adb('pull', dev_root, raw_root, timeout=1800)
    pulled_root = os.path.join(raw_root, os.path.basename(dev_root))
    if not os.path.isdir(pulled_root):
        pulled_root = raw_root
    n = len([1 for _dp, _dn, fs in os.walk(pulled_root) for f in fs if f.endswith('.bin')])
    log('  ✓ 已拉取 %d 个文件' % n)

    log('[4/5] 扫描 DEX 并跨 VMA 拼接')
    idx = index_dumps(pulled_root)
    found = scan_for_dex(idx)
    if not found:
        die('未在内存中找到 DEX。可能是: 1) app 未真正进入主界面 2) 壳版本变化。'
            ' 可尝试 --manual 模式或增大 --max-total-mb。')

    need = [d for d in found if not covered(idx, d['abs'], d['abs'] + d['size'])]
    if need:
        log('  %d 个 DEX 越界，补采 %d 段...' % (len(need), len(need)))
        lines2 = []
        for i, d in enumerate(need):
            cur = d['abs']
            limit = d['abs'] + d['size']
            while cur < limit:
                ve = vma_end(vmas, cur, limit)
                if ve <= cur:
                    ve = min(limit, cur + PAGE)
                lines2.append(dd_line(pid, cur, ve - cur, dev_root,
                                      'x%02d_%016x_%08x.bin' % (i, cur, ve - cur)))
                cur = ve
        push_and_run(build_script(lines2, dev_root, args.jobs), workdir, len(lines2), 'extra')
        adb('pull', dev_root, raw_root, timeout=1800)
        idx = index_dumps(pulled_root)

    out_dir = os.path.join(workdir, 'unpacked_dex')
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)

    results = []
    seen_sha = set()
    dup = 0
    for d in found:
        blob, gaps = assemble(idx, vmas, d['abs'], d['size'])
        if gaps:
            continue
        fixed, status = fix_and_validate(blob)
        if fixed is None:
            continue
        sha = hashlib.sha256(fixed).hexdigest()
        if sha in seen_sha:          # 同一份 DEX 在内存中的多个副本，去重
            dup += 1
            continue
        seen_sha.add(sha)
        st = dex_stats(fixed)
        results.append({'blob': fixed, 'addr': d['abs'], 'size': d['size'],
                        'ver': d['ver'], 'stats': st})
    if dup:
        log('  （已去除 %d 个内存中的重复副本）' % dup)
    if not results:
        die('拼接后没有可用的 DEX（可能内存布局变化）。请把 maps.txt 反馈给维护者。')

    results.sort(key=lambda r: -r['size'])
    log('')
    log('%-14s %10s %8s %9s %8s %8s %8s' % ('文件', '大小(B)', '类数', '方法数',
                                            '有代码', 'native', 'abstract'))
    manifest = []
    for i, r in enumerate(results, 1):
        name = 'classes%02d.dex' % i
        path = os.path.join(out_dir, name)
        open(path, 'wb').write(r['blob'])
        s = r['stats']
        log('%-14s %10d %8d %9d %8d %8d %8d' % (name, r['size'], s['classes'], s['methods'],
                                                s['with_code'], s['native'], s['abstract']))
        manifest.append({'file': name, 'size': r['size'], 'addr': '%016x' % r['addr'],
                         'dex_version': r['ver'], 'sha256': hashlib.sha256(r['blob']).hexdigest(),
                         **s})
    tot_m = sum(m['methods'] for m in manifest)
    tot_wc = sum(m['with_code'] for m in manifest)
    unexplained = sum(m['unexplained'] for m in manifest)
    log('')
    log('  合计: %d 个 DEX, %.1f MB, %d 类, %d 方法, 含代码 %d (%.1f%%)' % (
        len(manifest), sum(m['size'] for m in manifest) / 1048576.0,
        sum(m['classes'] for m in manifest), tot_m, tot_wc, 100.0 * tot_wc / max(tot_m, 1)))
    if unexplained:
        log('  ⚠ 有 %d 个方法既非 native 也非 abstract 却无代码体，请检查是否为抽取壳' % unexplained)
    elif tot_wc == 0:
        log('  ✗ 所有方法都没有代码体，说明取到的是抽取壳的空壳，请反馈 maps.txt')
    else:
        log('  ✓ 完整性校验通过: 无代码体的方法 100% 为 native/abstract，无抽取空洞')

    jadx = None
    manifest_jadx = {}
    if not args.no_jadx:
        log('')
        log('[5/5] jadx 反编译')
        jadx = find_jadx(args.jadx)
        if not jadx or not os.path.exists(jadx):
            log('  ⚠ 未找到 jadx，跳过反编译（可用 --jadx 指定路径）。DEX 已生成。')
            manifest_jadx = {}
        else:
            jh = find_java_home()
            env = dict(os.environ)
            if jh:
                env['JAVA_HOME'] = jh
                env['PATH'] = os.path.join(jh, 'bin') + os.pathsep + env.get('PATH', '')
            jadx_out = os.path.join(workdir, 'jadx_out')
            if os.path.isdir(jadx_out):
                shutil.rmtree(jadx_out, ignore_errors=True)
            dexes = [os.path.join(out_dir, m['file']) for m in manifest]
            cmd = [jadx, '-d', jadx_out, '--show-bad-code', '--no-imports',
                   '--threads-count', str(args.jadx_threads)] + dexes
            log('  运行 jadx（%d 个 dex，可能需要几分钟）...' % len(dexes))
            subprocess.run(cmd, env=env, capture_output=True, encoding='utf-8',
                           errors='replace', timeout=3600)
            src = os.path.join(jadx_out, 'sources')
            cnt = len([1 for _dp, _dn, fs in os.walk(src) for f in fs if f.endswith('.java')])
            log('  ✓ 反编译完成: %d 个 java 文件' % cnt)
            manifest_jadx = {'dir': jadx_out, 'java_files': cnt}



    meta = {'tool': 'zsxq_unpack.py', 'time': datetime.now().isoformat(timespec='seconds'),
            'package': pkg, 'pid': pid, 'device': SERIAL,
            'apk': args.apk, 'dex_count': len(manifest), 'dex': manifest,
            'jadx': manifest_jadx}
    mp = os.path.join(out_dir, 'unpack_manifest.json')
    json.dump(meta, open(mp, 'w', encoding='utf-8'), indent=1, ensure_ascii=False)

    if not args.keep_raw:
        log('')
        log('清理设备端与本地临时文件...')
        su('rm -rf ' + dev_root + ' /data/local/tmp/zsxq_unpack_*.sh')
        shutil.rmtree(raw_root, ignore_errors=True)
    else:
        log('保留原始内存镜像: ' + raw_root)

    return manifest, out_dir


PACKAGE = 'com.unnoo.quan'
MAIN_ACTIVITY = 'com.unnoo.quan.activities.StartActivity'


def main():
    global PACKAGE, MAIN_ACTIVITY
    ap = argparse.ArgumentParser(
        description='知识星球(通付盾加固)一键静默脱壳工具 —— 无需 frida',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apk', help='先安装指定 APK（可选，默认直接使用手机上已安装的版本）')
    ap.add_argument('--pkg', default=PACKAGE, help='包名（默认 %s）' % PACKAGE)
    ap.add_argument('--activity', default=MAIN_ACTIVITY, help='入口 Activity')
    ap.add_argument('--out', default=None, help='输出目录（默认 ./unpack_<时间戳>）')
    ap.add_argument('--manual', action='store_true', help='人工确认已进入主界面后再回车')
    ap.add_argument('--no-jadx', action='store_true', help='不执行 jadx 反编译')
    ap.add_argument('--jadx', default=None, help='jadx 可执行文件路径')
    ap.add_argument('--jadx-threads', type=int, default=8, help='jadx 线程数')
    ap.add_argument('--jobs', type=int, default=4, help='设备端并行转储进程数')
    ap.add_argument('--max-region-mb', type=int, default=64, help='单块区域大小上限(MB)')
    ap.add_argument('--max-total-mb', type=int, default=1200, help='转储总量上限(MB)')
    ap.add_argument('--keep-raw', action='store_true', help='保留内存原始镜像')
    ap.add_argument('--wait', type=int, default=45, help='启动后等待秒数（等壳解密完成）')
    args = ap.parse_args()
    PACKAGE = args.pkg
    MAIN_ACTIVITY = args.activity

    base = os.path.dirname(os.path.abspath(__file__))
    workdir = args.out or os.path.join(base, 'unpack_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    os.makedirs(workdir, exist_ok=True)
    if os.path.abspath(workdir) == base:
        die('输出目录不能是脚本所在目录，请用 --out 指定子目录')
    for sub in ('raw', 'unpacked_dex', 'jadx_out'):
        pth = os.path.join(workdir, sub)
        if os.path.isdir(pth):
            shutil.rmtree(pth, ignore_errors=True)

    log('=' * 62)
    log(' 知识星球 一键脱壳 (静默内存取证 / 无需 frida)')
    log('=' * 62)
    log('输出目录: ' + workdir)
    log('')
    log('[1/5] 环境检查')
    resolve_adb()
    check_root()

    if args.apk:
        apk_path = os.path.abspath(args.apk)
        if not os.path.exists(apk_path):
            die('APK 不存在: ' + apk_path)
        install_apk(apk_path)
    elif not is_installed(PACKAGE):
        die('手机上未安装 %s。请用 --apk 指定 APK 文件。' % PACKAGE)
    else:
        log('✓ 已安装 %s' % PACKAGE)

    log('')
    log('启动 App（等待壳解密 DEX）...')
    force_stop()
    time.sleep(1)
    if not launch():
        die('启动失败，请检查包名/Activity 名')
    pid = wait_for_pid(60)
    if not pid:
        die('App 进程未起来（PID 为空）')
    log('✓ 进程 PID = %d' % pid)
    if args.manual:
        input('  请确认 App 已进入主界面/登录页，然后按回车继续...')
    else:
        log('  等待 %d 秒让壳完成解密...' % args.wait)
        time.sleep(args.wait)
        if get_pid() is None:
            die('等待期间 App 进程退出（可能被杀）。请重试或用 --manual。')

    manifest, out_dir = extract(workdir, pid, PACKAGE, args)
    log('')
    log('=' * 62)
    log(' 完成 ✅')
    log(' DEX:  ' + out_dir)
    if not args.no_jadx:
        log(' 源码: ' + os.path.join(workdir, 'jadx_out', 'sources'))
    log(' 清单: ' + os.path.join(out_dir, 'unpack_manifest.json'))
    log('=' * 62)


if __name__ == '__main__':
    main()
