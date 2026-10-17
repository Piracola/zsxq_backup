#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tg_bot.py — 哨兵的 Telegram 机器人 (python-telegram-bot 22.x)

线程模型
  机器人跑在哨兵进程内的独立线程里, 自带一个 asyncio 事件循环 (run_polling)。
  哨兵是同步世界, 通过 push_text() 把内容投递进那个循环, 互不阻塞。
  所有 zsxq 侧的阻塞调用 (拉话题、扫码轮询、清理文件) 一律用 asyncio.to_thread
  丢出事件循环 —— 所以菜单在拉取期间依然响应。

命令
  /start /menu   主菜单      /groups  我的星球
  /status        哨兵状态    /login   微信扫码更新凭证
星球内按钮: 最新10条 / 自动推送开关 / 保留条数 / 返回
偏好 (自动推送、chat_id、groups 改动) 持久化在 watch.json, 重启不丢。
"""
import asyncio
import html as _html
import json
import os
import re
import threading
import time
import urllib.parse

import telegram
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto, LinkPreviewOptions)
from telegram.constants import ParseMode
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler,
                          CommandHandler, MessageHandler, filters)

from zsxq_archive import E_RE, log

MAXLEN = 3900                     # Telegram 单条上限 4096, 留出标签余量
BQ_OPEN, BQ_CLOSE = "<blockquote expandable>", "</blockquote>"
NOPREVIEW = LinkPreviewOptions(is_disabled=True)
MENU_TEXT = ("星球哨兵机器人\n\n🌍 我的星球 — 选星球看内容 / 设自动推送 / 设保留条数\n"
             "📊 哨兵状态 — 备份运行情况\n"
             "🔐 扫码更新凭证 — 凭证过期时点这里, 微信扫码即可\n\n"
             "星球名后面的标记: 🔄=新话题自动推送中 ✂️=只保留近期条数")


# ---------- 文本渲染 (HTML parse_mode) ----------

def _inline_html(s):
    """星球的内联标记 <e .../> → Telegram HTML; 其余文本转义。
    web 链接变 <a>, 加粗变 <b>, 其它取 title。"""
    out, pos, s = [], 0, s or ""
    for m in E_RE.finditer(s):
        out.append(_html.escape(s[pos:m.start()]))
        a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        t = a.get("type")
        title = _html.escape(urllib.parse.unquote(a.get("title", "")))
        if t == "web":
            href = _html.escape(urllib.parse.unquote(a.get("href", "")), quote=True)
            label = title or href
            if a.get("style") == "book":
                label = "📖 " + label
            out.append('<a href="%s">%s</a>' % (href, label) if href else label)
        elif t == "text_bold":
            out.append("<b>%s</b>" % title)
        else:
            out.append(title)
        pos = m.end()
    out.append(_html.escape(s[pos:]))
    return "".join(out)


def _plain(s):
    """去掉标记, 用于标题这类纯文本位置。"""
    return _html.unescape(re.sub(r"<[^>]+>", "", _inline_html(s)))


def _quote(s_html):
    """可折叠引用。入参必须已经是转义好的 HTML。"""
    return BQ_OPEN + s_html + BQ_CLOSE


def _clamp(text):
    """截到单条上限; 若切在引用内部, 补上闭合标签保证 HTML 合法。"""
    text = text[:MAXLEN]
    if text.count("<blockquote") > text.count(BQ_CLOSE):
        text += BQ_CLOSE
    return text


def _fmt_topic_brief(topic, gname, with_body=True):
    """一条话题 → 推送文本 (HTML)。with_body=False 只出一行摘要。"""
    tid = str(topic.get("topic_id"))
    ct = (topic.get("create_time") or "")[:16].replace("T", " ")
    author = ((topic.get("talk") or topic.get("answer")
               or topic.get("question") or {}).get("owner") or {}).get("name") or "?"
    title = (_plain(topic.get("title") or "").strip()
             or _plain((topic.get("talk") or {}).get("text") or "").strip()
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
    # 问答类: title 只是服务器截断的预览, 提问正文在 question.text 里
    q = topic.get("question") or {}
    a = topic.get("answer") or {}
    qraw = (q.get("text") or "").strip()
    araw = (a.get("text") or "").strip()
    braw = ((topic.get("talk") or {}).get("text") or "").strip()
    L = ["%s\n星球: %s" % (head, _html.escape(gname))]
    if qraw or q.get("owner"):
        who = _html.escape((q.get("owner") or {}).get("name") or "匿名提问")
        L += ["", "❓ %s:" % who, _quote(_inline_html(qraw) or "(提问正文为空)")]
    if braw and not qraw:
        L += ["", _quote(_inline_html(braw))]
    if araw:
        L += ["", "—— %s 的回答 ——" % _html.escape((a.get("owner") or {}).get("name") or "?"),
              _quote(_inline_html(araw))]
    L += ["", "https://wx.zsxq.com/dweb2/index/topic/%s" % tid]
    return "\n".join(L)


def _img_url(im):
    """一张图 → 最合适的 URL: large(约 800px) → original → thumbnail。"""
    if not isinstance(im, dict):
        return ""
    for key in ("large", "original", "thumbnail"):
        v = im.get(key)
        if isinstance(v, dict) and v.get("url"):
            return v["url"]
    return ""


def _topic_image_groups(topic):
    """按内容区块分组返回图片 URL, 顺序与正文一致。
    问答话题的提问图片在 question.images, 和 talk.images 不是同一个字段。"""
    groups = []

    def take(label, node):
        if not isinstance(node, dict):
            return
        urls, seen = [], set()
        for im in (node.get("images") or []):
            u = _img_url(im)
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
        if urls:
            groups.append((label, urls))

    take("提问附图", topic.get("question"))
    take("回答附图", topic.get("answer"))
    take("正文附图", topic.get("talk"))
    take("作业附图", topic.get("task"))
    take("解答附图", topic.get("solution"))
    return groups


class TgBot(threading.Thread):
    """Telegram 机器人线程。共享 Sentinel 实例读星球列表/备份状态,
    并把阻塞操作丢到线程池, 保持菜单响应。"""

    KEEP_CHOICES = [0, 50, 100, 200, 500, 1000]

    def __init__(self, sent):
        super().__init__(daemon=True, name="tg-bot")
        self.s = sent
        n = sent.cfg.get("notify") or {}
        self.token = n.get("telegram") or ""
        self.admins = {str(x) for x in (sent.cfg.get("bot_users") or [])}
        self.autopush_path = sent.cfg.get("_config_path") or "watch.json"
        self.autopush = dict(sent.cfg.get("bot_autopush") or {})
        self._chat_id = sent.cfg.get("bot_chat_id")
        self._login_running = False
        self._loop = None
        self.app = None
        if not self.token:
            return
        self.app = (Application.builder().token(self.token)
                    .post_init(self._post_init).build())
        a = self.app
        # 鉴权放在 -1 组: 不通过就 ApplicationHandlerStop, 后面的组一律不执行
        a.add_handler(MessageHandler(filters.ALL, self.guard), group=-1)
        a.add_handler(CallbackQueryHandler(self.guard), group=-1)
        a.add_handler(CommandHandler(["start", "menu"], self.cmd_menu))
        a.add_handler(CommandHandler("groups", self.cmd_groups))
        a.add_handler(CommandHandler("status", self.cmd_status))
        a.add_handler(CommandHandler("login", self.cmd_login))
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.cmd_menu))
        for pat, fn in (("^g:", self.cb_group), ("^l:", self.cb_latest),
                        ("^a:", self.cb_autopush), ("^k:", self.cb_keep),
                        ("^menu$", self.cb_menu), ("^groups$", self.cb_groups),
                        ("^status$", self.cb_status), ("^login$", self.cb_login),
                        ("^noop$", self.cb_noop)):
            a.add_handler(CallbackQueryHandler(fn, pattern=pat))

    # ---- 生命周期 ----

    async def _post_init(self, app):
        self._loop = asyncio.get_running_loop()      # 供哨兵线程跨线程投递
        try:
            await app.bot.set_my_commands([
                BotCommand("menu", "主菜单"), BotCommand("groups", "我的星球"),
                BotCommand("status", "哨兵状态"), BotCommand("login", "微信扫码更新凭证")])
        except Exception as e:
            log("[!] setMyCommands 失败: %s" % str(e)[:100])
        log("[+] Telegram 机器人已启动 (python-telegram-bot %s)" % telegram.__version__)

    def run(self):
        if not self.app:
            log("[!] 未配置 Telegram bot token, 机器人菜单不可用")
            return
        while True:                    # 兜底: 任何原因退出都重启, 别让菜单悄悄死掉
            try:
                self.app.run_polling(stop_signals=None,       # 不在主线程, 不能碰信号
                                     bootstrap_retries=-1,    # 启动期连不上就一直重试
                                     allowed_updates=["message", "callback_query"],
                                     timeout=30)
                return
            except Exception as e:
                log("[!] 机器人线程异常, 10s 后重启: %r" % e)
                time.sleep(10)

    def push_text(self, text):
        """哨兵 (同步线程) 调用: 把一条 HTML 文本投递进事件循环。"""
        chat = self.push_chat_id()
        if not (chat and self._loop and self.app):
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send(chat, text), self._loop)
        except Exception as e:
            log("[!] 推送投递失败: %s" % str(e)[:120])

    def push_topic(self, topic, gname):
        """哨兵 (同步线程) 调用: 推一条话题的正文, 再把提问/回答/正文里的图发出去。"""
        chat = self.push_chat_id()
        if not (chat and self._loop and self.app):
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send_topic(chat, topic, gname), self._loop)
        except Exception as e:
            log("[!] 推送投递失败: %s" % str(e)[:120])

    # ---- 发送 ----

    async def _send(self, chat_id, text, kb=None):
        try:
            await self.app.bot.send_message(chat_id, _clamp(text), parse_mode=ParseMode.HTML,
                                            link_preview_options=NOPREVIEW, reply_markup=kb)
        except Exception as e:
            log("[!] Telegram 发送失败: %s" % str(e)[:150])

    async def _send_topic(self, chat_id, topic, gname):
        await self._send(chat_id, _fmt_topic_brief(topic, gname))
        await self._send_images(chat_id, _topic_image_groups(topic))

    async def _send_images(self, chat_id, groups):
        """按区块发图。Telegram 相册一组最多 10 张; 组发失败就逐张补发。"""
        for label, urls in groups or []:
            for i in range(0, len(urls), 10):
                chunk = urls[i:i + 10]
                first = (i == 0)
                try:
                    if len(chunk) == 1:
                        await self.app.bot.send_photo(
                            chat_id, chunk[0], caption=label if first else None)
                    else:
                        media = [InputMediaPhoto(u) for u in chunk]
                        if first:
                            media[0] = InputMediaPhoto(chunk[0], caption=label)
                        await self.app.bot.send_media_group(chat_id, media)
                except Exception as e:
                    log("[!] 图片推送失败 (%s): %s" % (label, str(e)[:120]))
                    for j, u in enumerate(chunk):
                        try:
                            await self.app.bot.send_photo(
                                chat_id, u, caption=label if first and j == 0 else None)
                        except Exception as e2:
                            log("[!] 单张图片推送失败: %s" % str(e2)[:100])

    async def _show(self, query, text, kb=None):
        """按钮导航: 尽量就地改这条消息, 改不动就新发一条。"""
        try:
            await query.edit_message_text(_clamp(text), parse_mode=ParseMode.HTML,
                                          link_preview_options=NOPREVIEW, reply_markup=kb)
        except telegram.error.BadRequest as e:
            if "not modified" not in str(e).lower():
                await self._send(query.message.chat_id, text, kb)
        except Exception:
            await self._send(query.message.chat_id, text, kb)

    # ---- 鉴权 ----

    def _allowed(self, update):
        uid = update.effective_user.id if update.effective_user else None
        if self.admins:
            return str(uid) in self.admins
        chat = update.effective_chat
        return bool(chat and chat.type == "private")

    async def guard(self, update, context):
        if not self._allowed(update):
            uid = update.effective_user.id if update.effective_user else "?"
            if update.effective_chat:
                await self._send(update.effective_chat.id,
                                 "⛔ 你不在允许名单里 (你的 ID: %s)。"
                                 "请把它加进 watch.json 的 bot_users。" % uid)
            raise ApplicationHandlerStop
        self.remember_chat(update)
        return None

    def admins_or_any_chat(self):
        """自动推送的去处: 有白名单推第一个管理员, 否则推记住的私聊用户。"""
        return bool(self.admins or self._chat_id)

    def push_chat_id(self):
        if self.admins:
            return sorted(self.admins)[0]
        return self._chat_id

    def remember_chat(self, update):
        """没配白名单时, 记住第一个私聊用户作为自动推送对象 (存 watch.json)。"""
        if self.admins or self._chat_id:
            return
        chat = update.effective_chat
        if chat and chat.type == "private":
            self._chat_id = chat.id
            self.save_pref()

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

    # ---- 键盘 ----

    def kb_menu(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🌍 我的星球", callback_data="groups")],
            [InlineKeyboardButton("📊 哨兵状态", callback_data="status")],
            [InlineKeyboardButton("🔐 扫码更新凭证", callback_data="login")]])

    def kb_groups(self):
        rows = []
        for gid in sorted(self.s.gnames, key=lambda g: self.s.gnames[g]):
            marks = ""
            if self.autopush.get(str(gid)):
                marks += " 🔄"
            if (self.s.cfg.get("group_keep") or {}).get(str(gid)):
                marks += " ✂️"
            rows.append([InlineKeyboardButton(self.s.gnames[gid] + marks,
                                              callback_data="g:" + gid)])
        rows.append([InlineKeyboardButton("🔄=自动推送  ✂️=只留近期", callback_data="noop")])
        return InlineKeyboardMarkup(rows)

    def kb_group(self, gid):
        on = bool(self.autopush.get(str(gid)))
        keep = int((self.s.cfg.get("group_keep") or {}).get(str(gid)) or 0)
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📜 最新 10 条", callback_data="l:" + gid)],
            [InlineKeyboardButton("🔔 自动推送: 开" if on else "🔕 自动推送: 关",
                                  callback_data="a:" + gid)],
            [InlineKeyboardButton(("✂ 只留%d条 ✓" % keep) if keep else "✂ 不限量",
                                  callback_data="k:" + gid)],
            [InlineKeyboardButton("◀ 返回星球列表", callback_data="menu")]])

    # ---- 命令 ----

    async def cmd_menu(self, update, context):
        await self._send(update.effective_chat.id, MENU_TEXT, self.kb_menu())

    async def cmd_groups(self, update, context):
        await self._send(update.effective_chat.id,
                         "你加入的星球 (%d):" % len(self.s.gnames), self.kb_groups())

    async def cmd_status(self, update, context):
        await self._send(update.effective_chat.id,
                         await asyncio.to_thread(self.status_text))

    async def cmd_login(self, update, context):
        context.application.create_task(self.do_login(update.effective_chat.id))

    # ---- 按钮 ----

    async def cb_noop(self, update, context):
        await update.callback_query.answer()

    async def cb_menu(self, update, context):
        q = update.callback_query
        await q.answer()
        await self._show(q, MENU_TEXT, self.kb_menu())

    async def cb_groups(self, update, context):
        q = update.callback_query
        await q.answer()
        await self._show(q, "你加入的星球 (%d):" % len(self.s.gnames), self.kb_groups())

    async def cb_status(self, update, context):
        q = update.callback_query
        await q.answer()
        await self._show(q, await asyncio.to_thread(self.status_text))

    async def cb_login(self, update, context):
        q = update.callback_query
        await q.answer()
        context.application.create_task(self.do_login(q.message.chat_id))

    async def cb_group(self, update, context):
        q = update.callback_query
        await q.answer()
        gid = q.data.split(":", 1)[1]
        await self._show(q, "「%s」(%s)" % (self.s.gnames.get(gid, gid), gid),
                         self.kb_group(gid))

    async def cb_latest(self, update, context):
        q = update.callback_query
        await q.answer("正在拉取…")
        await self.push_latest(q.message.chat_id, q.data.split(":", 1)[1])

    async def cb_autopush(self, update, context):
        q = update.callback_query
        await q.answer()
        gid = q.data.split(":", 1)[1]
        await self._show(q, self.toggle_autopush(gid), self.kb_group(gid))

    async def cb_keep(self, update, context):
        q = update.callback_query
        await q.answer()
        gid = q.data.split(":", 1)[1]
        msg = await asyncio.to_thread(self.cycle_keep, gid)      # 清理文件, 别卡住循环
        await self._show(q, msg, self.kb_group(gid))

    # ---- 动作 ----

    def status_text(self):
        s = self.s
        state = list(s.state.values())          # 快照: 哨兵线程同时在改这个 dict
        lines = ["📊 哨兵状态",
                 "盯守星球: %d 个" % len(s.cfg["groups"]),
                 "备份话题: %d 条" % len(state),
                 "删除记录: %d 条" % sum(1 for v in state if v.get("deleted"))]
        auto = [s.gnames.get(g, g) for g, on in self.autopush.items() if on]
        lines.append("自动推送: %s" % (", ".join(auto) if auto else "未开启"))
        keeps = {g: int(v) for g, v in (s.cfg.get("group_keep") or {}).items() if v}
        lines.append("限量星球: %s" % (
            ", ".join("%s→%d条" % (s.gnames.get(g, g), n) for g, n in keeps.items())
            if keeps else "不限量"))
        lines.append("凭证: %s" % ("有效" if time.time() - s._last_auth_ok < 3600 else "待复核"))
        return "\n".join(lines)

    async def push_latest(self, chat_id, gid, n=10):
        gname = self.s.gnames.get(gid, gid)
        await self._send(chat_id, "正在拉取「%s」最新 %d 条…" % (gname, n))
        topics = (await asyncio.to_thread(self.s.latest_page, gid, n) or [])[:n]
        if not topics:
            await self._send(chat_id, "没有拉到话题 (可能星球没内容或请求失败)。")
            return
        for t in topics:
            await self._send(chat_id, _fmt_topic_brief(t, gname))
            await self._send_images(chat_id, _topic_image_groups(t))
            await asyncio.sleep(0.6)

    def toggle_autopush(self, gid):
        """开关自动推送。开启时把星球补进盯守列表 —— 不在 groups 里永远不会被轮询到。"""
        key = str(gid)
        self.autopush[key] = not self.autopush.get(key)
        if self.autopush[key]:
            groups = [g for g in (self.s.cfg.get("groups") or []) if g]
            if key not in groups:
                groups.append(key)
                self.s.cfg["groups"] = groups
        self.save_pref()
        gname = self.s.gnames.get(gid, gid)
        return "「%s」自动推送已%s%s" % (
            gname, "开启 🔔" if self.autopush[key] else "关闭 🔕",
            "\n(该星球已加入盯守列表)" if self.autopush[key] else "")

    def cycle_keep(self, gid):
        """循环切换保留条数: 不限量 → 50 → 100 → 200 → 500 → 1000 → 不限量。"""
        key, choices = str(gid), self.KEEP_CHOICES
        cur = int((self.s.cfg.get("group_keep") or {}).get(key) or 0)
        nxt = choices[(choices.index(cur) + 1) % len(choices)] if cur in choices else choices[1]
        gk = self.s.cfg.setdefault("group_keep", {})
        gk[key] = nxt
        if not nxt:
            gk.pop(key, None)
        self.save_pref()
        gname = self.s.gnames.get(gid, gid)
        if nxt:
            return "「%s」只保留最近 %d 条, 已清理旧话题 %d 条" % (
                gname, nxt, self.s.prune_group(gid, nxt))
        return "「%s」改为不限量 (已有内容不会找回)" % gname

    # ---- 扫码登录 ----

    async def do_login(self, chat_id):
        """整条扫码链路跑在机器人线程里, 阻塞步骤全部 to_thread。"""
        if self._login_running:
            await self._send(chat_id, "已有一个扫码流程在进行中, 请先完成或等它超时。")
            return
        self._login_running = True
        try:
            import zsxq_login as L
            await self._send(chat_id, "🔐 正在生成微信登录二维码…")
            uuid_, qr_url = await asyncio.to_thread(L.wx_new_qr)
            await self.app.bot.send_photo(
                chat_id, qr_url, caption="用手机微信扫这张图, 点【确认登录】(约4分钟有效)")
            code = await self._wait_scan(chat_id, uuid_, qr_url, L)
            await self._send(chat_id, "✅ 已确认, 正在换取新凭证…")
            data, aduid, resp_headers = await asyncio.to_thread(L.exchange_token, code)
            if not data.get("succeeded"):
                await self._send(chat_id, "❌ 换取凭证失败: %s"
                                 % json.dumps(data, ensure_ascii=False)[:200])
                return
            token, _ = L.extract_token(data, resp_headers)
            if not token:
                await self._send(chat_id, "❌ 响应里没找到凭证, 请看服务日志。")
                return
            me = ((data.get("resp_data") or {}).get("user") or {})
            await asyncio.to_thread(self._write_cred, token, aduid, me)
            if await asyncio.to_thread(self.s.hot_reload_cred):
                await self._send(chat_id, "✅ 登录成功: %s\n新凭证已生效, 哨兵继续盯守, 无需重启。"
                                 % me.get("name", "?"))
            else:
                await self._send(chat_id, "⚠️ 新凭证已写入但校验未通过, 请看服务日志。")
        except Exception as e:
            await self._send(chat_id, "❌ 登录流程出错: %s" % str(e)[:200])
            log("[!] 机器人扫码登录异常: %r" % e)
        finally:
            self._login_running = False

    async def _wait_scan(self, chat_id, uuid_, qr_url, L):
        """等扫码: 过期或取消就换新码重发, 最多 5 张。"""
        for attempt in range(1, 6):
            deadline, scanned = time.time() + 240, False
            while time.time() < deadline:
                st, code = await asyncio.to_thread(L.wx_poll_code, uuid_)
                if st == "ok":
                    return code
                if st == "scanned" and not scanned:
                    scanned = True
                    await self._send(chat_id, "👀 已扫码, 请在手机微信上点【确认登录】")
                if st == "canceled":
                    await self._send(chat_id, "❌ 已取消, 发新二维码")
                    break
                await asyncio.sleep(2)
            if attempt == 5:
                break
            uuid_, qr_url = await asyncio.to_thread(L.wx_new_qr)
            await self.app.bot.send_photo(
                chat_id, qr_url, caption="⏳ 二维码已过期, 这是新的一张 (第 %d 张), 请扫码" % attempt)
        raise TimeoutError("等多轮都没人扫码, 放弃")

    def _write_cred(self, token, aduid, me):
        with open(self.s.cred_path, "w", encoding="utf-8") as f:
            json.dump({"access_token": token, "aduid": aduid,
                       "login_way": "wechat_qr_bot",
                       "login_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "_user": me}, f, ensure_ascii=False, indent=1)
