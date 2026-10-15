#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tg_bot.py — 哨兵的 Telegram 菜单机器人

长轮询线程, 随哨兵进程启动 (zsxq_watch.py 里 TgBot(sent).start())。
通过传入的 Sentinel 实例读星球列表、复用备份与热替换凭证能力;
本模块自身不碰 zsxq API。

命令:
  /start /menu   主菜单
  /groups        列出账号加入的所有星球 (点星球名进入)
  /status        哨兵状态
  /login         微信扫码更新登录凭证
星球内按钮: 最新10条 / 自动推送开关 / 保留条数 / 返回。
偏好 (自动推送、chat_id、groups 改动) 持久化在 watch.json, 重启不丢。
"""
import html as _html
import json, os, re, threading, time, urllib.parse, urllib.request
from zsxq_archive import E_RE, render_text, log

TG_API = "https://api.telegram.org/bot%s/%s"


def _tg_call(token, method, payload=None, timeout=35):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(TG_API % (token, method), data=data)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


_BQ_RE = re.compile(r"^", re.M)


def _tg_quote(text):
    """转成 Telegram HTML 的可折叠引用 (<blockquote expandable>)。
    MarkdownV2 的转义规则太苛刻, 用 HTML parse_mode 省心: 只需转 <>&。"""
    return "<blockquote expandable>%s</blockquote>" % _BQ_RE.sub("&gt;", _html.escape(text))


def _fmt_topic_brief(topic, gname, with_body=True):
    """一条话题 → 推送文本 (HTML parse_mode)。
    with_body=False 时只出一行摘要 (列表用)。"""
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
    head = "💬 %s · %s · %s%s" % (_html.escape(author), ct, _html.escape(title[:60]),
                                  (" [" + "·".join(tags) + "]") if tags else "")
    if not with_body:
        return head
    # 问答类: 提问正文用 talk/question 字段里的全文 (title 只是服务器截断的预览),
    # 提问和回答都放进可折叠引用, 长文点开才展开
    q = topic.get("question") or {}
    a = topic.get("answer") or {}
    qtext = render_text(q.get("text") or "").strip()
    atext = render_text(a.get("text") or "").strip()
    body = render_text((topic.get("talk") or {}).get("text") or "").strip()
    L = ["%s\n星球: %s" % (head, _html.escape(gname))]
    if qtext or a.get("owner"):
        who = _html.escape((q.get("owner") or {}).get("name") or "匿名提问")
        L.append("")
        L.append("❓ %s:" % who)
        L.append(_tg_quote(qtext or "(提问正文为空)"))
    if body and not q.get("text"):
        L.append("")
        L.append(_tg_quote(body))
    if atext:
        L.append("")
        L.append("—— %s 的回答 ——" % _html.escape((a.get("owner") or {}).get("name") or "?"))
        L.append(_tg_quote(atext))
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
            text = text[:3900]
            # 截断可能切在 <blockquote> 内部, 补上闭合标签保证 HTML 合法
            if text.count("<blockquote") > text.count("</blockquote>"):
                text += "</blockquote>"
            p = {"chat_id": chat_id, "text": text,
                 "parse_mode": "HTML", "disable_web_page_preview": True}
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
