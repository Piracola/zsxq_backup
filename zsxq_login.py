#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zsxq_login.py — 微信扫码登录知识星球, 凭证直落 VPS

流程 (逆向自 wx.zsxq.com 前端, 2025-xx 实测可用):
  1. 请求微信开放平台扫码页, 拿 uuid 与二维码图片
  2. 二维码通过 Telegram 发给你 (sendPhoto, 图片直接用微信的 URL)
  3. 轮询 lp.open.weixin.qq.com 等你扫码确认, 拿到 code
  4. 生成随机 AES 密钥/IV, 用星球网页版内置 RSA 公钥包裹密钥,
     调 POST /v2/access_tokens 换 zsxq_access_token
  5. 写入 cred.json (保留原有 aduid 字段结构), 哨兵自动热加载

用法 (在 VPS 上):
  python3 zsxq_login.py                    # 读 watch.json 里的 Telegram 配置
  python3 zsxq_login.py --qr qr.png        # 本地调试: 二维码存文件, 不发 Telegram
依赖: pip3 install pycryptodome
"""
import argparse, base64, hashlib, json, os, re, sys, time, urllib.parse, urllib.request, uuid

APPID = "wxa8d63c1238079ec4"          # 知识星球网页版微信开放平台 appid
REDIRECT_URI = "https://wx.zsxq.com/load"
API = "https://api.zsxq.com"
WEB_X_VERSION = "2.98.0"              # 网页版 v2 接口版本号
WEB_V3_X_VERSION = "3.24.0"           # 登录接口走 /v3/, 版本号要换成 v3 系
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PUB_KEY = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEArbJvWdwi4w96rNjQQQTs
qzMefjZVP5CrZ+5vNj/qG5zefzqZa9o87pAWzH3MG/HW+0k9DzHv33cxIk4yQcy6
NJb/QuDCLYUoCjkoefa6rienCTruyYNFhFt/JCCTNd2UecS914cbr+5YKp81mPGj
QuVBwu8akI7NVZLKe+vufhn0/sNeWzmn4v/kKWwsrWy1q+8LfKGidFiNMJtRDTHG
kRDOKW8M8sIgNowp1ot/m00QB65j1B/rqAsTLod0bSe0W++v5SEkNh+XrEO9/d+c
zsDOeUL+NqhO6+EPjwJVxHn0PnvgaNKQ51OpniNV9WLtEyjv/A674zot2zA9VEX/
MwIDAQAB
-----END PUBLIC KEY-----"""
TOKEN_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}"
                      r"-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}_[0-9A-Fa-f]{16}")


def log(m):
    print(time.strftime("[%H:%M:%S] ") + m, flush=True)


def http(url, data=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=data)
    req.add_header("User-Agent", WEB_UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), dict(r.headers)


# ---------- Telegram ----------

def tg(cfg, method, payload):
    tok, chat = cfg.get("telegram"), cfg.get("telegram_chat_id")
    if not tok or not chat:
        return None
    try:
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/%s" % (tok, method), data=body)
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        log("[!] Telegram 发送失败: %s" % str(e)[:120])
        return None


def tg_send_text(cfg, text):
    return tg(cfg, "sendMessage", {"chat_id": cfg["telegram_chat_id"],
                                   "text": text[:4000]})


def tg_send_photo(cfg, photo_url, caption):
    return tg(cfg, "sendPhoto", {"chat_id": cfg["telegram_chat_id"],
                                 "photo": photo_url, "caption": caption[:1000]})


# ---------- 微信扫码 ----------

def wx_new_qr():
    """开一个新二维码会话, 返回 (uuid, qr_image_url)。"""
    q = urllib.parse.urlencode({
        "appid": APPID, "scope": "snsapi_login",
        "redirect_uri": REDIRECT_URI, "state": uuid.uuid4().hex,
        "login_type": "jssdk", "self_redirect": "true"})
    body, _ = http("https://open.weixin.qq.com/connect/qrconnect?" + q)
    html = body.decode("utf-8", "replace")
    # uuid 就在二维码 img 的 src 里: /connect/qrcode/<uuid>
    m = re.search(r'/connect/qrcode/([A-Za-z0-9_=\-]+)', html)
    if not m:
        raise RuntimeError("扫码页里没找到 uuid, 页面结构可能变了:\n" + html[:300])
    return m.group(1), "https://open.weixin.qq.com/connect/qrcode/%s" % m.group(1)


def wx_poll_code(uuid_):
    """轮询扫码状态。返回 ('wait'|'scanned'|'canceled', code)。"""
    q = "uuid=%s&last=408&_%d" % (uuid_, int(time.time() * 1000))
    try:
        body, _ = http("https://long.open.weixin.qq.com/connect/l/qrconnect?" + q,
                       timeout=30,
                       headers={"Referer": "https://open.weixin.qq.com/connect/qrconnect",
                                "Accept": "*/*"})
    except Exception:
        return "wait", None
    text = body.decode("utf-8", "replace")
    m_err = re.search(r"wx_errcode['\"]?\s*[:=]\s*['\"]?(\d+)", text)
    m_code = re.search(r"wx_code['\"]?\s*[:=]\s*['\"]([A-Za-z0-9_=\-]*)['\"]", text)
    err = m_err.group(1) if m_err else "408"
    code = m_code.group(1) if m_code else ""
    # 405 = 已确认并下发 code; 404 = 已扫码未确认; 408 = 继续等; 402/403 = 过期/取消
    if err == "405" and code:
        return "ok", code
    if err == "404":
        return "scanned", None
    if err in ("402", "403"):
        return "canceled", None
    return "wait", None


def wait_scan(uuid_, cfg, qr_url, local_qr_path=None):
    """轮询直到确认/取消/过期(自动换新码重发)。返回 code。"""
    attempt = 1
    while True:
        deadline = time.time() + 240          # 微信二维码有效期约 4 分钟
        scanned = False
        while time.time() < deadline:
            st, code = wx_poll_code(uuid_)
            if st == "ok":
                return code
            if st == "scanned" and not scanned:
                scanned = True
                log("[·] 已扫码, 请在手机上确认")
                if cfg:
                    tg_send_text(cfg, "👀 已扫码, 请在手机微信上点【确认登录】")
            if st == "canceled":
                log("[·] 你取消了登录, 重新出新码")
                if cfg:
                    tg_send_text(cfg, "❌ 已取消, 发新二维码")
                break
            time.sleep(2)
        attempt += 1
        if attempt > 5:
            raise RuntimeError("等多轮都没人扫码, 放弃")
        log("[·] 二维码过期, 第 %d 次重发" % attempt)
        uuid_, qr_url = wx_new_qr()
        if cfg:
            tg_send_photo(cfg, qr_url, "⏳ 二维码已过期, 这是新的一张 (第 %d 张), 请扫码" % attempt)
        elif local_qr_path:
            img, _ = http(qr_url)
            open(local_qr_path, "wb").write(img)
            log("      新二维码已写回 %s" % local_qr_path)


# ---------- code 换 token (AES+RSA 加密接口) ----------

def exchange_token(code):
    from Crypto.Cipher import AES, PKCS1_v1_5
    from Crypto.PublicKey import RSA
    from Crypto.Util.Padding import pad, unpad
    from Crypto.Random import get_random_bytes

    # 前端 generateAESKey(): CryptoJS 随机 16 字节转 base64 取前 16 个字符, 本身作为 UTF-8 密钥
    key = base64.b64encode(get_random_bytes(16)).decode()[:16]
    iv = base64.b64encode(get_random_bytes(16)).decode()[:16]

    payload = json.dumps({"req_data": {"client": "DWeb",
                                       "wechat": {"auth": {"appid": APPID, "code": code}}}},
                         separators=(",", ":"))
    cipher = AES.new(key.encode(), AES.MODE_CBC, iv.encode())
    body = base64.b64encode(cipher.encrypt(pad(payload.encode(), 16)))

    rsa = PKCS1_v1_5.new(RSA.import_key(PUB_KEY))
    x_key = base64.b64encode(rsa.encrypt(key.encode())).decode()
    x_iv = base64.b64encode(iv.encode()).decode()

    # 注意: 网页版前端 getUrl() 会把匹配 /^access_tokens$/ 的路由重写到 /v3/,
    # 且 X-Version 必须用 v3 系版本号 —— 用 v2 + 2.98.0 会拿到解不开的固定错误密文。
    url = API + "/v3/access_tokens"
    ts, rid = int(time.time()), str(uuid.uuid4())
    aduid = str(uuid.uuid4())                 # 网页版 XAduid 就是随机 uuid, 首次生成后固定复用
    sig = hashlib.sha1(("%s %d %s" % (url, ts, rid)).encode()).hexdigest()
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Origin": "https://wx.zsxq.com",
        "Referer": "https://wx.zsxq.com/",
        "X-Key": x_key, "X-IV": x_iv,
        "X-Request-Id": rid, "X-Timestamp": str(ts),
        "X-Signature": sig, "X-Aduid": aduid,
        "X-Version": WEB_V3_X_VERSION,
    }
    body, resp_headers = http(url, data=body, headers=headers, timeout=30)
    text = body.decode("utf-8", "replace").strip()
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", text):
        raise RuntimeError("登录接口返回的不是 base64 密文: " + text[:200])
    raw = base64.b64decode(text)
    plain = unpad(AES.new(key.encode(), AES.MODE_CBC, iv.encode()).decrypt(raw), 16)
    data = json.loads(plain.decode("utf-8"))
    return data, aduid, resp_headers


def extract_token(data, resp_headers):
    """从解密后的响应(或 Set-Cookie)里找 access_token。"""
    def walk(node):
        if isinstance(node, str):
            return node if TOKEN_RE.fullmatch(node) else None
        if isinstance(node, dict):
            for k in ("access_token", "token"):
                v = node.get(k)
                if isinstance(v, str) and TOKEN_RE.fullmatch(v):
                    return v
            for v in node.values():
                t = walk(v)
                if t:
                    return t
        if isinstance(node, list):
            for v in node:
                t = walk(v)
                if t:
                    return t
        return None

    tok = walk(data)
    if tok:
        return tok, data
    cookie = (resp_headers.get("Set-Cookie") or "")
    m = re.search(r"zsxq_access_token=([0-9A-Fa-f\-_]+)", cookie)
    if m:
        return m.group(1), data
    return None, data


def main():
    ap = argparse.ArgumentParser(description="微信扫码登录知识星球, 凭证写入 cred.json")
    ap.add_argument("--config", default="watch.json", help="读 Telegram 配置的文件")
    ap.add_argument("--cred", default="cred.json")
    ap.add_argument("--qr", default=None, help="本地调试: 二维码存到这个文件, 不走 Telegram")
    ap.add_argument("--out-info", default=None, help="调试: 把换token的完整响应存这里")
    a = ap.parse_args()

    cfg = {}
    if os.path.exists(a.config):
        cfg = (json.load(open(a.config, encoding="utf-8")) or {}).get("notify") or {}

    log("[1/4] 生成扫码二维码...")
    uuid_, qr_url = wx_new_qr()
    log("      uuid=%s" % uuid_)
    if a.qr:
        img, _ = http(qr_url)
        open(a.qr, "wb").write(img)
        log("      二维码已存 %s (用手机微信扫它)" % a.qr)
    else:
        if not cfg.get("telegram"):
            log("[x] watch.json 里没配 Telegram, 无法发码。先填 notify.telegram / telegram_chat_id")
            return 1
        r = tg_send_photo(cfg, qr_url, "🔐 扫码登录知识星球\n用手机微信扫这张图, 并点【确认登录】(5分钟内有效)")
        log("      二维码已发 Telegram: %s" % ("成功" if r else "失败"))

    log("[2/4] 等待扫码...")
    code = wait_scan(uuid_, cfg if not a.qr else None, qr_url, local_qr_path=a.qr)
    log("[+] 拿到微信 code: %s..." % code[:12])

    log("[3/4] 换取星球凭证...")
    data, aduid, resp_headers = exchange_token(code)
    if a.out_info:
        json.dump(data, open(a.out_info, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if not data.get("succeeded"):
        log("[x] 换取失败: %s" % json.dumps(data, ensure_ascii=False)[:300])
        if cfg.get("telegram"):
            tg_send_text(cfg, "❌ 登录失败: %s" % json.dumps(data, ensure_ascii=False)[:200])
        return 1
    token, _ = extract_token(data, resp_headers)
    if not token:
        log("[x] 响应里没找到 access_token, 完整响应已存 --out-info 供分析")
        return 1

    log("[4/4] 写入 %s ..." % a.cred)
    old = {}
    if os.path.exists(a.cred):
        try:
            old = json.load(open(a.cred, encoding="utf-8"))
        except Exception:
            pass
    json.dump({"access_token": token, "aduid": aduid,
               "login_way": "wechat_qr", "login_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "_user": ((data.get("resp_data") or {}).get("user") or {})},
              open(a.cred, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    me = ((data.get("resp_data") or {}).get("user") or {}).get("name", "?")
    log("[✓] 登录成功: %s (token %s...)" % (me, token[:12]))
    if cfg.get("telegram") and not a.qr:
        tg_send_text(cfg, "✅ 星球登录成功: %s\n新凭证已写入, 哨兵会自动用上" % me)
    return 0


if __name__ == "__main__":
    sys.exit(main())
