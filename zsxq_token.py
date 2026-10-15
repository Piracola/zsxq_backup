#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zsxq_token.py - 从已 root 的安卓设备静默提取知识星球凭证并校验

原理（均经实测验证）:
  App 把登录态存在受通付盾 AZAPP 内核级透明加密保护的私有目录里, 磁盘上拿不到明文。
  但 token 一定以明文存在于进程内存中(HTTP 请求头/OkHttp/CookieJar/GC 堆)。
  本脚本用 root 直读 /proc/<pid>/mem 定位它 —— 不 ptrace、不注入、无常驻进程,
  因此不会触发壳的反调试。

用法:
  python zsxq_token.py                 # 提取 + 校验
  python zsxq_token.py --no-verify     # 只提取
  python zsxq_token.py --json out.json # 保存凭证
"""
import argparse, hashlib, json, os, re, subprocess, sys, time, uuid
import urllib.request, urllib.error

PACKAGE = "com.unnoo.quan"
API_BASE = "https://api.zsxq.com"
PAGE = 4096
TOKEN_RE = re.compile(rb"[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}_[0-9A-F]{16}")
ADUID_RE = re.compile(rb"00000000[0-9a-f]{64}")

# 跳过巨大且无意义的堆结构, 控制读取量
# 只排除纯粹的 GC 位图/标记栈; 必须保留 region space / main space ——
# Java String 就在这些堆里, 早期误排除它们会导致找不到 token。
SKIP = re.compile(r"bitmap|mark stack|card table|allocation stack|live stack|thread local|gc mark", re.I)
KEEP = re.compile(r"libc_malloc|LinearAlloc|bionic|\.bss|dalvik-DEX")


def sh(cmd, timeout=180):
    p = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout)
    return p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def find_pid(adb):
    out, _ = sh('"%s" shell "su -c \'pidof %s\'"' % (adb, PACKAGE))
    pid = out.strip().split()
    return pid[0] if pid else None


def read_maps(adb, pid):
    """解析 /proc/<pid>/maps, 返回候选可读区域。"""
    out, _ = sh('"%s" shell "su -c \'cat /proc/%s/maps\'"' % (adb, pid))
    regions = []
    for line in out.splitlines():
        # 注意: 区域名可能含空格(如 "[anon:dalvik-main space (region space)]"),
        # 必须整体按 5 段切分, 否则会被截断而误过滤掉真正的堆。
        parts = line.split(None, 5)
        if len(parts) < 5:
            continue
        m = re.match(r"^([0-9a-f]+)-([0-9a-f]+)$", parts[0])
        if not m:
            continue
        start, end = int(m.group(1), 16), int(m.group(2), 16)
        perms = parts[1]
        path = parts[5] if len(parts) > 5 else ""
        if not perms.startswith("r"):
            continue
        if not (path == "" or path.startswith("[anon:") or ".bss" in path):
            continue
        if SKIP.search(path):
            continue
        size = end - start
        # 大堆优先, 小的也保留; 过滤掉超小碎片
        if size < PAGE:
            continue
        regions.append({"start": start, "size": size, "path": path})
    # 优先扫描很可能命中字符串的区域, 再扫其余
    regions.sort(key=lambda r: (0 if KEEP.search(r["path"]) else 1, r["size"]))
    return regions


# token 最可能待在哪儿: Java 堆 / dex 线性分配区 / native malloc 与 .bss。
# 注意别把 "dalvik-free list large object space" 配进来 —— 那是纯空闲链表, 没有活对象,
# 却有 512MB, 白读。
HOT = re.compile(r"dalvik-main space|dalvik-non moving|dalvik-zygote"
                 r"|dalvik-large object space|LinearAlloc|libc_malloc|\.bss", re.I)
HOT_RANK = [(re.compile(r"dalvik-main space", re.I), 0),
            (re.compile(r"dalvik-non moving|dalvik-zygote|dalvik-large object space", re.I), 1),
            (re.compile(r"LinearAlloc|libc_malloc|\.bss", re.I), 2)]

TMP = "/data/local/tmp"
SCRIPT = TMP + "/zs.sh"
TOKEN_SRE = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
                       r"-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}_[0-9A-Fa-f]{16}$")


def build_scan_script(pid, regions, out_file, prog_file, done_file):
    """设备端脚本: 逐段 dd 出 /proc/<pid>/mem 交给 grep。

    两处和早先版本不同:
      * 命中行直接写进设备上的 out_file, 不走管道回传 —— 本机超时或掉线都不会
        把已经扫出来的东西丢掉, 重连后 cat 一下就行。
      * 每段开始前把进度写进 prog_file, 本机可以看着它走。
    """
    lines = ["#!/system/bin/sh", "PID=%s" % pid, "TOTAL=%d" % len(regions), "i=0", "{"]
    for r in regions:
        lines.append("  i=$((i+1)); echo $i/$TOTAL > %s" % prog_file)
        # timeout: 实测有少数段(如 zygote space)会让 read(/proc/pid/mem) 一直不返回,
        # 卡住整条流水线。按 2MB/s 给个下限 15 秒的期限, 到点跳过这一段继续。
        to = max(15, int(r["size"] / 2097152))
        lines.append("  timeout %d dd if=/proc/$PID/mem bs=%d skip=%d count=%d 2>/dev/null" %
                     (to, PAGE, r["start"] // PAGE, r["size"] // PAGE))
    # 单次遍历同时抓 token 与设备号
    lines.append("} | grep -a -o -E '[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
                 "-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}_[0-9A-Fa-f]{16}|00000000[0-9a-f]{64}'"
                 " > %s 2>/dev/null" % out_file)
    lines.append("echo done > %s" % done_file)
    return "\n".join(lines) + "\n"


def tally(lines):
    tc, ac = {}, {}
    for line in lines:
        line = line.strip()
        if TOKEN_SRE.match(line):
            k = line.upper()
            tc[k] = tc.get(k, 0) + 1
        elif line.startswith("00000000") and len(line) == 72:
            ac[line] = ac.get(line, 0) + 1
    return (sorted(tc.items(), key=lambda kv: -kv[1]),
            sorted(ac.items(), key=lambda kv: -kv[1]))


def _rank(r):
    for rx, p in HOT_RANK:
        if rx.search(r["path"]):
            return p
    return 9


def run_wave(adb, pid, tag, regs, timeout, verbose):
    """把一段区域表推上去跑, 返回 grep 出来的原始行。"""
    out_file = "%s/zs_%s.out" % (TMP, tag)
    prog_file = "%s/zs_%s.p" % (TMP, tag)
    done_file = "%s/zs_%s.d" % (TMP, tag)
    local = os.path.join(os.environ.get("TEMP", "."), "zsxq_scan.sh")
    with open(local, "w", newline="\n") as f:
        f.write(build_scan_script(pid, regs, out_file, prog_file, done_file))
    sh('"%s" push "%s" %s' % (adb, local, SCRIPT))
    try:
        os.remove(local)
    except OSError:
        pass
    # nohup + & : 本机这一步随时可以走开, 设备上照跑
    sh('"%s" shell "su -c \'rm -f %s %s %s; nohup sh %s >/dev/null 2>&1 &\'"' %
       (adb, out_file, prog_file, done_file, SCRIPT))

    t0, last, done = time.time(), "", False
    while time.time() - t0 < timeout:
        time.sleep(5)
        o, _ = sh('"%s" shell "su -c \'cat %s %s 2>/dev/null\'"' %
                  (adb, prog_file, done_file))
        cur = " ".join(o.split())
        if cur != last and verbose:
            last = cur
            print("      %s  %.0fs" % (cur or "等待中", time.time() - t0), flush=True)
        if "done" in o.split():
            done = True
            break
    if verbose and not done:
        print("      [!] %d 秒未跑完, 先把已扫到的部分取回来" % timeout)
    o, _ = sh('"%s" shell "su -c \'cat %s 2>/dev/null\'"' % (adb, out_file))
    sh('"%s" shell "su -c \'rm -f %s %s %s %s\'"' % (adb, SCRIPT, out_file, prog_file, done_file))
    return [l for l in o.splitlines() if l.strip()]


def extract(adb, pid, verbose=True, timeout=1800, verify=None):
    regions = read_maps(adb, pid)
    if verbose:
        print("  候选内存段 %d 个, 合计 %.0f MB" %
              (len(regions), sum(r["size"] for r in regions) / 1048576))
    hot = sorted([r for r in regions if HOT.search(r["path"])], key=lambda r: (_rank(r), -r["size"]))
    cold = sorted([r for r in regions if not HOT.search(r["path"])], key=lambda r: -r["size"])
    if verbose:
        print("  先扫 %d 段 / %.0f MB (堆与分配区), 兜底 %d 段 / %.0f MB" %
              (len(hot), sum(r["size"] for r in hot) / 1048576,
               len(cold), sum(r["size"] for r in cold) / 1048576))

    hits = []
    for tag, regs in (("hot", hot), ("cold", cold)):
        if not regs:
            continue
        if verbose:
            print("  [%s] %d 段 ..." % (tag, len(regs)), flush=True)
        hits += run_wave(adb, pid, tag, regs, timeout, verbose)
        tokens, aduids = tally(hits)
        if verbose and tokens:
            print("      命中候选 token %d 个" % len(tokens), flush=True)
        if not tokens:
            continue
        ad = aduids[0][1] if aduids else None
        if verify is None:
            return tokens, aduids
        for cnt, tok in tokens:          # 逐个拿服务端验, 验过就收工, 不必再扫兜底那批
            if verify(tok, ad):
                return [(cnt, tok)], aduids
    return tally(hits)


def api_call(token, aduid, path, version="2.88.0"):
    url = API_BASE + path
    ts, rid = int(time.time()), str(uuid.uuid4())
    sig = hashlib.sha1(("%s %d %s" % (url, ts, rid)).encode()).hexdigest()
    req = urllib.request.Request(url)
    for k, v in [("X-Aduid", aduid or rid), ("User-Agent", "xiaomiquan/5.35.2 Android/Phone/14"),
                 ("X-Version", version), ("Authorization", token), ("X-Request-Id", rid),
                 ("X-Timestamp", str(ts)), ("X-Signature", sig), ("X-Product", "zsxq"),
                 ("Cookie", "zsxq_access_token=" + token)]:
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"error": repr(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adb", default=os.environ.get("ADB", "adb"))
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--scan-timeout", type=int, default=1800, help="单批内存扫描的等待秒数")
    ap.add_argument("--json")
    a = ap.parse_args()

    print("[1/3] 定位进程 ...")
    pid = find_pid(a.adb)
    if not pid:
        print("  [x] 未找到 %s 进程, 请先启动 App" % PACKAGE)
        return 1
    print("  pid = %s" % pid)

    print("[2/3] 静默扫描内存 (root 直读 /proc/pid/mem, 不注入) ...")

    def verify(tok, ad):
        st, body = api_call(tok, ad, "/v2/users/self")
        u = (body.get("resp_data") or {}).get("user") or {}
        ok = st == 200 and bool(u.get("name"))
        print("      试 %s...%s -> %s" % (tok[:8], tok[-8:], "有效" if ok else "无效"), flush=True)
        return ok

    tokens, aduids = extract(a.adb, pid, timeout=a.scan_timeout,
                             verify=None if a.no_verify else verify)
    if not tokens:
        print("  [x] 未在内存中找到 token。可能 App 未处于登录态, 或已被系统回收内存。")
        return 1
    token = tokens[0][1]
    aduid = aduids[0][1] if aduids else None
    print("\n  token  = %s   (出现 %d 次)" % (token, tokens[0][0]))
    if aduid:
        print("  aduid  = %s" % aduid)

    if a.no_verify:
        result = {"access_token": token, "aduid": aduid}
    else:
        print("\n[3/3] 校验凭证 ...")
        st, body = api_call(token, aduid, "/v2/users/self")
        user = (body.get("resp_data") or {}).get("user") or {}
        if st == 200 and user.get("name"):
            print("  [OK] 身份验证通过: %s (user_id=%s)" % (user.get("name"), user.get("user_id")))
            st2, b2 = api_call(token, aduid, "/v3/groups?count=100", "3.14.0")
            groups = (b2.get("resp_data") or {}).get("groups") or []
            print("  已加入星球: %d 个" % len(groups))
            for g in groups[:20]:
                print("     - %s  %s" % (g.get("group_id"), g.get("name")))
            result = {"access_token": token, "aduid": aduid,
                      "user": user, "groups": groups}
        else:
            print("  [x] 校验失败: HTTP %s %s" % (st, str(body)[:200]))
            return 1

    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print("\n  凭证已保存: %s" % a.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
