#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zsxq_watch.py — 知识星球云端哨兵

  * 每 poll_interval 秒看一眼各星球最新话题, 发现新话题立即全量备份
    (raw JSON 先落盘 → 图片/附件 → Markdown), 内容被删之前已经拿回本地。
  * 每 check_interval 秒复查近期已归档话题:
      - 话题被删除  → 连续两轮确认后写入 deleted/ 并发 IM 告警
      - 内容被编辑  → 修订版另存 raw/gid/tid__rev_N.json, 发告警
      - 评论数下降  → 发告警 (评论被删的前兆)
  * 凭证失效自动告警 (6 小时冷却), 凭证本身仍需手机端提取:
      python zsxq_token.py --json cred.json
  * IM 告警渠道: 企业微信机器人 / 钉钉机器人 / Telegram / Bark / 通用 webhook

用法:
  python zsxq_watch.py --cred cred.json --group 12345678901234 --out zsxq_backup
  python zsxq_watch.py --once          # 只跑一轮 (配合 cron 也行)
配置写在 watch.json (首次运行自动生成模板), 告警渠道全在里面。
"""
import argparse, json, os, random, sys, threading, time, urllib.parse, urllib.request
from zsxq_archive import (Client, download, collect_images, collect_files,
                          render_md, render_text, topic_filename, safe_name, log)

API = "https://api.zsxq.com"

DEFAULT_CFG = {
    "groups": [],              # 要盯的星球 ID 列表; 命令行 --group 优先
    "poll_interval": 30,       # 新话题轮询间隔 (秒); 实际会加 ±25% 抖动, 避免请求过于规律
    "check_interval": 600,     # 滚动复查节奏 (秒): 每轮检查"距上次检查最久"的一批
    "recent_days": 3,          # 只复查最近 N 天归档的话题
    "confirm_gap": 150,        # 删除判定: 两轮确认的最小间隔 (秒), 防瞬时误报
    "notify_new": False,       # 新话题是否也发通知
    "bot_users": [],           # Telegram 机器人允许的 Telegram 用户 ID (数字); 空则允许所有私聊用户
    "group_keep": {},          # gid -> 只保留最近 N 条 (机器人里设置); 0/缺省 = 不限制
    "notify": {
        "wecom": "",           # 企业微信机器人: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
        "dingtalk": "",        # 钉钉机器人: https://oapi.dingtalk.com/robot/send?access_token=xxx
        "telegram": "",        # bot token, 如 123456:ABC-DEF...
        "telegram_chat_id": "",
        "bark": "",            # 如 https://api.day.app/你的key
        "generic": ""          # 任意 HTTP POST, 收到 {"title":..., "text":...}
    }
}


def load_cfg(path, cli_groups):
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(path):
        try:
            user = json.load(open(path, encoding="utf-8"))
            cfg.update(user)
            cfg["notify"] = dict(DEFAULT_CFG["notify"])
            cfg["notify"].update(user.get("notify") or {})
        except Exception as e:
            log("[!] 配置文件解析失败, 使用默认配置: %s" % e)
    else:
        json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        log("[+] 已生成配置模板 %s — 告警渠道请填进里面的 notify 字段" % path)
    if cli_groups:
        cfg["groups"] = cli_groups
    return cfg


# ---------- Telegram 机器人 (菜单交互) ----------
#
# 菜单命令:
#   /start /menu          主菜单
#   /groups               列出账号加入的所有星球 (点星球名进入)
#   /status               哨兵状态
# 进入某个星球后的按钮:
#   [最新 10 条]  立即推送该星球最近 10 条话题的正文
#   [自动推送 ✓/✗] 新话题出现时自动把内容推到 Telegram
#   [◀ 返回列表]  回到星球列表
# 自动推送偏好存在 watch.json 的 "bot_autopush" 里, 重启不丢。

TG_API = "https://api.telegram.org/bot%s/%s"


def _tg_call(token, method, payload=None, timeout=35):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(TG_API % (token, method), data=data)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _fmt_topic_brief(topic, gname, with_body=True):
    """一条话题 → 推送文本。with_body=False 时只出一行摘要 (列表用)。"""
    tid = str(topic.get("topic_id"))
    ct = (topic.get("create_time") or "")[:16].replace("T", " ")
    author = ((topic.get("talk") or topic.get("answer")
               or topic.get("question") or {}).get("owner") or {}).get("name") or "?"
    title = (render_text(topic.get("title") or "").strip()
             or render_text((topic.get("talk") or {}).get("text") or "").strip()
             or "(无标题)")
    tags = []
    if topic.get("digested"):
        tags.append("★精华")
    if topic.get("sticky"):
        tags.append("📌置顶")
    head = "💬 %s · %s · %s%s" % (author, ct, title[:60],
                                  (" [" + "·".join(tags) + "]") if tags else "")
    if not with_body:
        return head
    body = render_text((topic.get("talk") or {}).get("text")
                       or (topic.get("question") or {}).get("text")
                       or (topic.get("answer") or {}).get("text") or "").strip()
    ans = render_text((topic.get("answer") or {}).get("text") or "").strip()
    L = ["%s\n星球: %s" % (head, gname)]
    if body and not topic.get("answer"):
        L.append("")
        L.append(body[:2500])
    if ans:
        L.append("")
        L.append("—— 回答 ——")
        L.append(ans[:2500])
    L.append("")
    L.append("https://wx.zsxq.com/dweb2/index/topic/%s" % tid)
    return "\n".join(L)


class TgBot(threading.Thread):
    """Telegram 长轮询机器人。跑在哨兵进程内的独立线程里,
    通过共享的 Sentinel 实例读星球列表/复用它的备份与推送能力。"""

    def __init__(self, sent):
        super().__init__(daemon=True, name="tg-bot")
        self.s = sent
        n = sent.cfg.get("notify") or {}
        self.token = n.get("telegram") or ""
        self.admins = set(str(x) for x in sent.cfg.get("bot_users") or [])
        self.autopush_path = sent.cfg.get("_config_path") or "watch.json"
        self.offset = 0
        # gid -> 是否自动推送; 持久化在 watch.json
        self.autopush = dict(sent.cfg.get("bot_autopush") or {})
        self._chat_id = sent.cfg.get("bot_chat_id")  # 未配白名单时记住的私聊用户
        self._login_running = False

    # ---- 持久化 ----

    def save_pref(self):
        try:
            p = self.autopush_path
            data = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
            data["bot_autopush"] = self.autopush
            data["bot_chat_id"] = self._chat_id
            data["groups"] = self.s.cfg.get("groups") or []
            json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception as e:
            log("[!] 机器人偏好保存失败: %s" % e)

    # ---- 权限 ----

    def allowed(self, msg):
        uid = str(((msg.get("from") or {}).get("id")) or "")
        if self.admins:
            return uid in self.admins
        return msg.get("chat", {}).get("type") == "private"

    def admins_or_any_chat(self):
        """自动推送的去处: 有白名单推第一个管理员, 否则推记住的私聊用户。"""
        return bool(self.admins or self._chat_id)

    def push_chat_id(self):
        if self.admins:
            return sorted(self.admins)[0]
        return self._chat_id

    def remember_chat(self, msg):
        """没配 bot_users 时, 记住第一个私聊用户作为自动推送对象 (存 watch.json)。"""
        if self.admins or self._chat_id:
            return
        if msg.get("chat", {}).get("type") == "private":
            self._chat_id = msg["chat"]["id"]
            self.save_pref()

    # ---- 发送 ----

    def send(self, chat_id, text, kb=None):
        try:
            p = {"chat_id": chat_id, "text": text[:4000],
                 "disable_web_page_preview": True}
            if kb:
                p["reply_markup"] = {"inline_keyboard": kb}
            _tg_call(self.token, "sendMessage", p)
        except Exception as e:
            log("[!] Telegram 发送失败: %s" % str(e)[:120])

    def send_long(self, chat_id, texts, kb=None):
        """分段发送一串文本 (Telegram 单条上限 4096)。"""
        for i, t in enumerate(texts):
            self.send(chat_id, t, kb if i == len(texts) - 1 else None)
            if i < len(texts) - 1:
                time.sleep(0.6)

    def set_menu(self):
        try:
            _tg_call(self.token, "setMyCommands", {"commands": [
                {"command": "menu", "description": "主菜单"},
                {"command": "groups", "description": "我的星球"},
                {"command": "status", "description": "哨兵状态"},
                {"command": "login", "description": "微信扫码更新登录凭证"}]})
        except Exception:
            pass

    # ---- 键盘 ----

    def kb_groups(self):
        kb = []
        for gid in sorted(self.s.gnames, key=lambda g: self.s.gnames[g]):
            marks = ""
            if self.autopush.get(str(gid)):
                marks += " 🔄"
            if (self.s.cfg.get("group_keep") or {}).get(str(gid)):
                marks += " ✂️"
            kb.append([{"text": self.s.gnames[gid] + marks,
                        "callback_data": "g:" + gid}])
        kb.append([{"text": "🔄=自动推送  ✂️=只留近期", "callback_data": "noop"}])
        return kb

    KEEP_CHOICES = [0, 50, 100, 200, 500, 1000]

    def kb_group(self, gid):
        gname = self.s.gnames.get(gid, gid)
        on = bool(self.autopush.get(str(gid)))
        keep = int((self.s.cfg.get("group_keep") or {}).get(str(gid)) or 0)
        row2 = [{"text": ("✂ 只留%d条 ✓" % keep) if keep else "✂ 不限量",
                 "callback_data": "k:" + gid}]
        return [
            [{"text": "📜 最新 10 条", "callback_data": "l:" + gid}],
            [{"text": ("🔔 自动推送: 开" if on else "🔕 自动推送: 关"),
              "callback_data": "a:" + gid}],
            row2,
            [{"text": "◀ 返回星球列表", "callback_data": "menu"}],
        ]

    # ---- 动作 ----

    def menu(self, chat_id):
        kb = [[{"text": "🌍 我的星球", "callback_data": "groups"}],
              [{"text": "📊 哨兵状态", "callback_data": "status"}],
              [{"text": "🔐 扫码更新凭证", "callback_data": "login"}]]
        self.send(chat_id,
                  "星球哨兵机器人\n\n🌍 我的星球 — 选星球看内容 / 设自动推送 / 设保留条数\n"
                  "📊 哨兵状态 — 备份运行情况\n"
                  "🔐 扫码更新凭证 — 凭证过期时点这里, 微信扫码即可\n\n"
                  "星球名后面的标记: 🔄=新话题自动推送中 ✂️=只保留近期条数", kb)

    def status_text(self):
        s = self.s
        lines = ["📊 哨兵状态",
                 "盯守星球: %d 个" % len(s.cfg["groups"]),
                 "备份话题: %d 条" % len(s.state),
                 "删除记录: %d 条" % sum(
                     1 for v in s.state.values() if v.get("deleted"))]
        auto = [self.s.gnames.get(g, g) for g, on in self.autopush.items() if on]
        lines.append("自动推送: %s" % (", ".join(auto) if auto else "未开启"))
        keeps = {g: int(v) for g, v in (self.s.cfg.get("group_keep") or {}).items() if v}
        lines.append("限量星球: %s" % (
            ", ".join("%s→%d条" % (self.s.gnames.get(g, g), n) for g, n in keeps.items())
            if keeps else "不限量"))
        lines.append("凭证: %s" % ("有效" if time.time() - s._last_auth_ok < 3600 else "待复核"))
        return "\n".join(lines)

    def push_latest(self, chat_id, gid, n=10):
        gname = self.s.gnames.get(gid, gid)
        self.send(chat_id, "正在拉取「%s」最新 %d 条…" % (gname, n))
        topics = self.s.latest_page(gid, count=n)[:n]
        if not topics:
            self.send(chat_id, "没有拉到话题 (可能星球没内容或请求失败)。")
            return
        msgs = [_fmt_topic_brief(t, gname) for t in topics]
        self.send_long(chat_id, msgs)

    def toggle_autopush(self, chat_id, gid):
        key = str(gid)
        self.autopush[key] = not self.autopush.get(key)
        # 自动推送的前提是这个星球真的在被盯守: 不在 groups 里就加进去并落盘,
        # 否则 poll_new 永远不会路过它, 推送开关是个摆设
        if self.autopush[key]:
            groups = [g for g in (self.s.cfg.get("groups") or []) if g]
            if key not in groups:
                groups.append(key)
                self.s.cfg["groups"] = groups
        self.save_pref()
        gname = self.s.gnames.get(gid, gid)
        self.send(chat_id, "「%s」自动推送已%s%s" % (
            gname, "开启 🔔" if self.autopush[key] else "关闭 🔕",
            "\n(该星球已加入盯守列表)" if self.autopush[key] and key in self.s.cfg["groups"] else ""),
            self.kb_group(gid))

    def cycle_keep(self, chat_id, gid):
        """循环切换保留条数: 不限量 → 50 → 100 → 200 → 500 → 1000 → 不限量。"""
        key = str(gid)
        choices = self.KEEP_CHOICES
        cur = int((self.s.cfg.get("group_keep") or {}).get(key) or 0)
        nxt = choices[(choices.index(cur) + 1) % len(choices)] if cur in choices else choices[1]
        gk = self.s.cfg.setdefault("group_keep", {})
        if nxt:
            gk[key] = nxt
        else:
            gk.pop(key, None)
        # 配置改动写回 watch.json
        try:
            p = self.autopush_path
            data = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}
            data["group_keep"] = {k: v for k, v in gk.items() if v}
            json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        except Exception as e:
            log("[!] group_keep 写回失败: %s" % e)
        gname = self.s.gnames.get(gid, gid)
        if nxt:
            removed = self.s.prune_group(gid, nxt)
            msg = "「%s」只保留最近 %d 条, 已清理旧话题 %d 条" % (gname, nxt, removed)
        else:
            msg = "「%s」改为不限量 (已有内容不会找回)" % gname
        self.send(chat_id, msg, self.kb_group(gid))

    # ---- 扫码登录 ----

    def start_login(self, chat_id):
        if self._login_running:
            self.send(chat_id, "已有一个扫码流程在进行中, 请先完成或等它超时。")
            return
        self._login_running = True
        threading.Thread(target=self._login_worker, args=(chat_id,),
                         daemon=True, name="tg-login").start()

    def _login_worker(self, chat_id):
        """跑 zsxq_login 的扫码换凭证流程, 成功后热替换哨兵凭证。"""
        try:
            import zsxq_login as L
            self.send(chat_id, "🔐 正在生成微信登录二维码…")
            uuid_, qr_url = L.wx_new_qr()
            r = L.tg_send_photo({"telegram": self.token,
                                 "telegram_chat_id": chat_id},
                                qr_url, "用手机微信扫这张图, 点【确认登录】(约4分钟有效)")
            if not r:
                self.send(chat_id, "二维码发送失败, 请稍后再试。")
                return
            code = L.wait_scan(uuid_, {"telegram": self.token,
                                       "telegram_chat_id": chat_id},
                               qr_url, local_qr_path=None)
            self.send(chat_id, "✅ 已确认, 正在换取新凭证…")
            data, aduid, resp_headers = L.exchange_token(code)
            if not data.get("succeeded"):
                self.send(chat_id, "❌ 换取凭证失败: %s" % json.dumps(data, ensure_ascii=False)[:200])
                return
            token, _ = L.extract_token(data, resp_headers)
            if not token:
                self.send(chat_id, "❌ 响应里没找到凭证, 请看服务日志。")
                return
            me = ((data.get("resp_data") or {}).get("user") or {})
            with open(self.s.cred_path, "w", encoding="utf-8") as f:
                json.dump({"access_token": token, "aduid": aduid,
                           "login_way": "wechat_qr_bot",
                           "login_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "_user": me}, f, ensure_ascii=False, indent=1)
            if self.s.hot_reload_cred():
                self.send(chat_id, "✅ 登录成功: %s\n新凭证已生效, 哨兵继续盯守, 无需重启。" % me.get("name", "?"))
            else:
                self.send(chat_id, "⚠️ 新凭证已写入但校验未通过, 请看服务日志。")
        except Exception as e:
            self.send(chat_id, "❌ 登录流程出错: %s" % str(e)[:200])
            log("[!] 机器人扫码登录异常: %r" % e)
        finally:
            self._login_running = False

    # ---- 主循环 ----

    def run(self):
        if not self.token:
            log("[!] 未配置 Telegram bot token, 机器人菜单不可用")
            return
        self.set_menu()
        log("[+] Telegram 机器人已启动 (命令: /menu /groups /status)")
        while True:
            try:
                d = _tg_call(self.token, "getUpdates",
                             {"offset": self.offset, "timeout": 30,
                              "allowed_updates": ["message", "callback_query"]},
                             timeout=40)
                for u in d.get("result") or []:
                    self.offset = max(self.offset, u["update_id"] + 1)
                    try:
                        self.handle(u)
                    except Exception as e:
                        log("[!] 机器人处理异常: %r" % e)
            except Exception as e:
                log("[!] 机器人轮询异常, 5s 后重试: %s" % str(e)[:120])
                time.sleep(5)

    def handle(self, u):
        cb = u.get("callback_query")
        if cb:
            msg = cb.get("message") or {}
            chat = msg.get("chat", {}).get("id")
            # 权限比对必须用 cb["from"] (按按钮的人)。
            # msg["from"] 是发这条消息的机器人自己 —— 用它会把所有按钮点击都判成未授权。
            actor = cb.get("from") or {}
            probe = dict(msg)
            probe["from"] = actor
            if not self.allowed(probe):
                self.send(chat, "⛔ 你不在允许名单里 (你的 ID: %s)。"
                          "请把它加进 watch.json 的 bot_users。" % actor.get("id"))
                return
            self.remember_chat(probe)
            try:
                _tg_call(self.token, "answerCallbackQuery", {"callback_query_id": cb["id"]})
            except Exception:
                pass
            data = cb.get("data") or ""
            if data.startswith("g:"):
                gid = data[2:]
                self.send(chat, "「%s」(%s)" % (self.s.gnames.get(gid, gid), gid),
                          self.kb_group(gid))
            elif data.startswith("l:"):
                self.push_latest(chat, data[2:])
            elif data.startswith("a:"):
                self.toggle_autopush(chat, data[2:])
            elif data.startswith("k:"):
                self.cycle_keep(chat, data[2:])
            elif data == "login":
                self.start_login(chat)
            elif data == "groups":
                self.send(chat, "你加入的星球 (%d):" % len(self.s.gnames), self.kb_groups())
            elif data == "status":
                self.send(chat, self.status_text())
            elif data == "menu":
                self.menu(chat)
            return

        msg = u.get("message")
        if not msg:
            return
        chat = msg.get("chat", {}).get("id")
        if not self.allowed(msg):
            self.send(chat, "⛔ 你不在允许名单里 (你的 ID: %s)。"
                      "请把它加进 watch.json 的 bot_users。" % (msg.get("from") or {}).get("id"))
            return
        self.remember_chat(msg)
        text = (msg.get("text") or "").strip()
        if text.startswith(("/start", "/menu")):
            self.menu(chat)
        elif text.startswith("/groups"):
            self.send(chat, "你加入的星球 (%d):" % len(self.s.gnames), self.kb_groups())
        elif text.startswith("/status"):
            self.send(chat, self.status_text())
        elif text.startswith("/login"):
            self.start_login(chat)
        elif text.startswith("/"):
            self.menu(chat)


class Sentinel:
    def __init__(self, cred_path, out, cfg):
        self.out, self.cfg = out, cfg
        self.cred_path = cred_path
        os.makedirs(out, exist_ok=True)
        self.state_path = os.path.join(out, "state.json")
        self.state = json.load(open(self.state_path, encoding="utf-8")) if os.path.exists(self.state_path) else {}
        cred = json.load(open(cred_path, encoding="utf-8"))
        self.cli = Client(cred["access_token"], cred.get("aduid"),
                          sleep=max(0.2, cfg.get("api_sleep", 1.0)))
        self.gnames = {}          # gid -> 名称
        self._auth_fail_ts = 0.0  # 凭证告警冷却
        self._last_auth_ok = 0.0  # 最近一次凭证校验通过的时间
        self.bot = None           # Telegram 机器人线程 (main 里启动)

    # ---------- 基础 ----------

    def save_state(self):
        json.dump(self.state, open(self.state_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)

    def auth_ok(self):
        me = self.cli.whoami()
        if me.get("name"):
            self._last_auth_ok = time.time()
            return True
        if time.time() - self._auth_fail_ts > 6 * 3600:
            self._auth_fail_ts = time.time()
            self.notify("⚠️ 星球哨兵: 凭证已失效",
                        "whoami 校验失败, 无法继续备份。\n"
                        "请在手机上重新运行: python zsxq_token.py --json cred.json\n"
                        "然后上传替换云端的 cred.json, 服务会自动恢复。")
        return False

    def refresh_groups(self):
        for g in self.cli.groups():
            self.gnames[str(g.get("group_id"))] = g.get("name") or ""

    def hot_reload_cred(self):
        """扫码登录成功后热替换凭证, 不必重启服务。"""
        try:
            cred = json.load(open(self.cred_path, encoding="utf-8"))
            tok = cred.get("access_token")
            if not tok:
                return False
            self.cli = Client(tok, cred.get("aduid"),
                              sleep=max(0.2, self.cfg.get("api_sleep", 1.0)))
            me = self.cli.whoami()
            if me.get("name"):
                self._last_auth_ok = time.time()
                self.gnames = {}
                return True
        except Exception as e:
            log("[!] 凭证热替换失败: %s" % e)
        return False

    def prune_group(self, gid, keep):
        """只保留某星球最近 keep 条备份, 超出的旧话题删除文件并移出状态。"""
        keys = [k for k in self.state
                if k.startswith(gid + "/") and not self.state[k].get("deleted")]
        if len(keys) <= keep:
            return 0
        keys.sort(key=lambda k: self.state[k].get("time") or "")
        gone, removed = keys[:-keep], 0
        for k in gone:
            tid = k.split("/", 1)[1]
            for p in (os.path.join(self.out, "raw", gid, "%s.json" % tid),
                      os.path.join(self.out, "deleted", gid, "%s.json" % tid)):
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            v = self.state.pop(k, None)
            md = v and v.get("file")
            if md:
                p = os.path.join(self.out, md)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            removed += 1
        self.save_state()
        self.rebuild_index()
        return removed

    # ---------- 告警 ----------

    def _post(self, url, payload=None, timeout=15):
        try:
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            req = urllib.request.Request(url, data=data)
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except Exception as e:
            log("[!] 告警发送失败: %s" % str(e)[:120])
            return 0

    def notify(self, title, text=""):
        n = self.cfg.get("notify") or {}
        body = (title + "\n" + text).strip()
        if n.get("wecom"):
            self._post(n["wecom"], {"msgtype": "text",
                                    "text": {"content": body[:2000]}})
        if n.get("dingtalk"):
            self._post(n["dingtalk"], {"msgtype": "markdown", "markdown": {
                "title": title, "text": "## %s\n\n%s" % (title, text)[:18000]}})
        if n.get("telegram") and n.get("telegram_chat_id"):
            self._post("https://api.telegram.org/bot%s/sendMessage" % n["telegram"],
                       {"chat_id": n["telegram_chat_id"],
                        "text": body[:4000], "disable_web_page_preview": True})
        if n.get("bark"):
            self._post("%s/%s/%s" % (n["bark"].rstrip("/"),
                                     urllib.parse.quote(title),
                                     urllib.parse.quote(text or " ")[:500]))
        if n.get("generic"):
            self._post(n["generic"], {"title": title, "text": text})

    @staticmethod
    def topic_link(gid, tid):
        return "https://wx.zsxq.com/dweb2/index/topic/%s (星球 %s)" % (tid, gid)

    # ---------- 新话题备份 ----------

    def latest_page(self, gid, count=20):
        d = self.cli.req("/v2/groups/%s/topics?scope=all&count=%d" % (gid, count))
        if not d.get("succeeded"):
            log("    [!] %s 最新列表拉取失败: %s" % (gid, str(d.get("error"))[:80]))
            return []
        return ((d.get("resp_data") or {}).get("topics")) or []

    def archive_topic(self, gid, gname, topic):
        """全量备份单个话题 (与 zsxq_archive.py 同一套落地结构)。"""
        tid = str(topic.get("topic_id"))
        gdir = os.path.join(self.out, safe_name(gname, 50) + "_" + gid)
        rawdir = os.path.join(self.out, "raw", gid)
        os.makedirs(gdir, exist_ok=True)
        os.makedirs(rawdir, exist_ok=True)

        comments = self.cli.comments(tid) if (topic.get("comments_count") or 0) > 0 else []
        rawpath = os.path.join(rawdir, "%s.json" % tid)
        with open(rawpath, "w", encoding="utf-8") as f:
            json.dump({"topic": topic, "comments": comments, "_group": gname,
                       "_fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)

        url2name, file_names = [], []
        for url, base in collect_images(topic) + collect_images(comments):
            if download(url, os.path.join(gdir, "images", base)):
                url2name.append((url, base))
        for fid, nm, size, dur in collect_files(topic):
            dd = self.cli.req("/v2/files/%s/download_url" % fid)
            u = (dd.get("resp_data") or {}).get("download_url")
            if u and download(u, os.path.join(gdir, "files", nm)):
                file_names.append((u, nm, size, dur))
        if url2name or file_names:
            with open(rawpath, "w", encoding="utf-8") as f:
                json.dump({"topic": topic, "comments": comments, "_group": gname,
                           "_img_map": url2name, "_files": file_names,
                           "_fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False)

        fname = topic_filename(topic)
        with open(os.path.join(gdir, fname), "w", encoding="utf-8") as f:
            f.write(render_md(topic, comments, dict(url2name), gname))

        self.state["%s/%s" % (gid, tid)] = {
            "file": os.path.relpath(os.path.join(gdir, fname), self.out),
            "time": topic.get("create_time"),
            "mtime": topic.get("modify_time") or "",
            "cc": topic.get("comments_count") or 0}
        return rawpath

    def poll_new(self):
        for gid in self.cfg["groups"]:
            gname = self.gnames.get(gid, gid)
            for topic in self.latest_page(gid):
                tid = str(topic.get("topic_id"))
                key = "%s/%s" % (gid, tid)
                if key in self.state:
                    continue
                log("    [新话题] %s %s" % (tid, (topic.get("title") or "")[:30]))
                self.archive_topic(gid, gname, topic)
                self.save_state()
                if self.bot and self.autopush_on(gid) and self.bot.admins_or_any_chat():
                    self.bot.send(self.bot.push_chat_id(),
                                  _fmt_topic_brief(topic, gname))
                if self.cfg.get("notify_new"):
                    self.notify("📥 新话题已备份",
                                "星球: %s\n标题: %s\n%s" % (
                                    gname, (topic.get("title") or "")[:60],
                                    self.topic_link(gid, tid)))

    # ---------- 删除 / 篡改监测 ----------

    def autopush_on(self, gid):
        """该星球是否开了机器人自动推送; 没配 admin 时只推给第一个私聊过机器人的用户。"""
        return bool(self.bot and self.bot.autopush.get(str(gid)))


    def recent_keys(self, days):
        now = time.time()
        out = []
        for key, v in self.state.items():
            if v.get("deleted"):
                continue
            t = v.get("time") or ""
            try:
                ts = time.mktime(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                continue
            if (now - ts) / 86400 <= days:
                out.append(key)
        return out

    def check_topics(self):
        gap = self.cfg.get("confirm_gap", 150)
        days = self.cfg.get("recent_days", 3)
        keys = self.recent_keys(days)
        if not keys:
            return
        # 滚动复查: 每轮只查"距上次检查最久"的一批, 避免话题多时单轮请求数爆炸
        budget = max(10, int(self.cfg.get("check_interval", 300)
                             / max(0.2, self.cfg.get("api_sleep", 1.0)) * 0.4))
        keys.sort(key=lambda k: self.state[k].get("last_check", 0))
        for key in keys[:budget]:
            gid, tid = key.split("/", 1)
            self.state[key]["last_check"] = time.time()
            d = self.cli.req("/v2/topics/%s" % tid)
            if d.get("succeeded"):
                self.on_detail(gid, tid, key, ((d.get("resp_data") or {}).get("topic") or {}))
            else:
                self.on_missing(gid, tid, key, d, gap)
        self.save_state()

    def on_missing(self, gid, tid, key, resp, gap):
        v = self.state[key]
        now = time.time()
        seen = v.get("gone_seen") or 0
        if not seen:
            v["gone_seen"] = now
            log("    [?] 话题疑似消失, 待二次确认: %s" % tid)
            return
        if now - seen < gap:
            return
        # 两轮确认仍不存在 → 判定被删除
        rec = {"topic_id": tid, "group_id": gid, "group": self.gnames.get(gid, gid),
               "title": "", "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "api_resp": resp}
        rawp = os.path.join(self.out, "raw", gid, "%s.json" % tid)
        if os.path.exists(rawp):
            raw = json.load(open(rawp, encoding="utf-8"))
            rec["title"] = (raw.get("topic") or {}).get("title") or ""
            rec["raw_snapshot"] = raw
        ddir = os.path.join(self.out, "deleted", gid)
        os.makedirs(ddir, exist_ok=True)
        with open(os.path.join(ddir, "%s.json" % tid), "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=1)
        v["deleted"] = True
        v.pop("gone_seen", None)
        log("    [!] 话题确认被删除: %s %s" % (tid, rec["title"][:30]))
        self.notify("🚨 话题被删除 (已备份在手)",
                    "星球: %s\n标题: %s\n备份: %s\n详情: %s" % (
                        rec["group"], rec["title"][:80] or "(无标题)",
                        rawp, self.topic_link(gid, tid)))

    def on_detail(self, gid, tid, key, topic):
        v = self.state[key]
        if v.get("gone_seen"):          # 复活了 → 撤销疑似
            v.pop("gone_seen", None)
            log("    [✓] 话题恢复可见: %s" % tid)
        mtime = topic.get("modify_time") or ""
        cc = topic.get("comments_count") or 0
        first = "mtime" not in v
        if first:                        # 首轮建立基线, 不告警
            v["mtime"], v["cc"] = mtime, cc
            return
        changed = []
        if mtime and v.get("mtime") and mtime != v["mtime"]:
            changed.append("内容被编辑 (%s → %s)" % (v["mtime"][:19], mtime[:19]))
        if cc < (v.get("cc") or 0):
            changed.append("评论数下降 (%d → %d)" % (v["cc"], cc))
        if not changed:
            return
        # 修订快照另存, 原始 raw 不动
        gname = self.gnames.get(gid, gid)
        n = 1
        while os.path.exists(os.path.join(self.out, "raw", gid, "%s__rev_%d.json" % (tid, n))):
            n += 1
        revp = os.path.join(self.out, "raw", gid, "%s__rev_%d.json" % (tid, n))
        comments = self.cli.comments(tid) if cc > 0 else []
        with open(revp, "w", encoding="utf-8") as f:
            json.dump({"topic": topic, "comments": comments, "_group": gname,
                       "_fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)
        v["mtime"], v["cc"] = mtime, cc
        log("    [!] 话题有变动: %s (%s)" % (tid, "; ".join(changed)))
        self.notify("✏️ 话题内容有变动",
                    "星球: %s\n标题: %s\n%s\n快照: %s" % (
                        gname, (topic.get("title") or "")[:80],
                        "; ".join(changed), revp))

    # ---------- 主循环 ----------

    def rebuild_index(self):
        try:
            from zsxq_archive import build_index
            groups = [{"group_id": g, "name": n} for g, n in self.gnames.items()]
            build_index(self.out, groups, self.state)
        except Exception as e:
            log("[!] 索引重建失败: %s" % e)

    def run_once(self):
        if not self.auth_ok():
            log("[x] 凭证失效, 本轮跳过")
            return
        if not self.gnames:
            self.refresh_groups()
            missing = [g for g in self.cfg["groups"] if g not in self.gnames]
            if missing:
                log("[x] 这些星球不在账号里: %s" % ", ".join(missing))
                self.cfg["groups"] = [g for g in self.cfg["groups"] if g not in missing]
        new = 0
        before = len(self.state)
        self.poll_new()
        new = len(self.state) - before
        if new:
            self.rebuild_index()
        self.check_topics()
        # 机器人里给某些星球设了"只留最近 N 条"的话, 每轮收尾清理一次
        for gid, keep in (self.cfg.get("group_keep") or {}).items():
            if keep:
                self.prune_group(str(gid), int(keep))
        log("[·] 本轮完成: 新增备份 %d, 状态共 %d 条" % (new, len(self.state)))

    def run(self):
        log("[+] 哨兵启动: 盯 %d 个星球, 轮询 %ds±25%%, 复查 %ds" % (
            len(self.cfg["groups"]), self.cfg["poll_interval"], self.cfg["check_interval"]))
        if not self.cfg["groups"]:
            log("[x] 没有配置星球 (--group 或 watch.json 的 groups)"); return 1
        next_run = 0.0
        while True:
            # 到点才干活: 实际节奏 = poll_interval ±25%, 不是死循环硬刷
            now = time.time()
            if now < next_run:
                time.sleep(min(next_run - now, 5))
                continue
            base = self.cfg["poll_interval"]
            wait = base * random.uniform(0.75, 1.25)   # 抖动: 别让服务器看到钟表一样的节奏
            try:
                self.run_once()
            except Exception as e:
                log("[!] 轮询异常: %r" % e)
                wait = 60                              # 出异常就多歇一会再试
            next_run = time.time() + wait


def main():
    ap = argparse.ArgumentParser(description="知识星球云端哨兵: 即时备份 + 删除监测 + IM 告警")
    ap.add_argument("--cred", default="cred.json")
    ap.add_argument("--out", default="zsxq_backup")
    ap.add_argument("--config", default="watch.json")
    ap.add_argument("--group", action="append", default=[], help="要盯的星球 ID, 可多次")
    ap.add_argument("--once", action="store_true", help="只跑一轮就退出 (配合 cron)")
    a = ap.parse_args()
    cfg = load_cfg(a.config, a.group)
    cfg["_config_path"] = a.config
    w = Sentinel(a.cred, a.out, cfg)
    w.bot = TgBot(w)
    w.bot.start()
    if a.once:
        w.run_once()
        return 0
    # 启动确认: 服务真的跑起来了会发这条, 收到即代表进程活着、凭证有效与否一眼可知
    w.notify("🟢 星球哨兵已启动",
             "盯守 %d 个星球, 轮询 %ds±25%%, 机器人菜单已就绪 (/menu)。" % (
                 len(w.cfg["groups"]), w.cfg["poll_interval"]))
    return w.run()


if __name__ == "__main__":
    sys.exit(main())
