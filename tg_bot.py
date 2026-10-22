#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tg_bot.py — 哨兵的 Telegram 机器人 (python-telegram-bot 22.x)

线程模型
  机器人跑在哨兵进程内的独立线程里, 自带一个 asyncio 事件循环 (run_polling)。
  哨兵是同步世界, 通过 push_text()/push_topic()/push_comment_change() 把内容投递
  进那个循环, 互不阻塞。所有 zsxq 侧的阻塞调用 (拉话题、扫码轮询、读备份) 一律用
  asyncio.to_thread 丢出事件循环 —— 菜单在拉取期间依然能点。

命令
  /menu /start  主菜单      /groups  我的星球      /status 哨兵状态
  /login        微信扫码续凭证
  直接发几个字 = 按标题搜本地备份

星球里能做的
  翻备份清单 → 任意一条话题的正文 / 全部评论 / 改动逐行对比
  拉最新 10 条 (直接从服务器看, 不等备份)
  开自动推送、选评论提醒档位 (都提醒/只看新增/只看减少/全关)、保留条数、抓取节奏

偏好 (自动推送、chat_id、groups、保留条数、抓取设置) 持久化在 watch.json, 重启不丢。
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

from tg_topics import COMMENT_PAGE, LIST_PAGE, Archive, snap, snap_list
from zsxq_archive import (E_RE, collect_files, log, save_json, topic_blocks,
                          topic_gone)

MAXLEN = 3900                     # Telegram 单条上限 4096, 留出标签余量
BQ_OPEN, BQ_CLOSE = "<blockquote expandable>", "</blockquote>"
NOPREVIEW = LinkPreviewOptions(is_disabled=True)
TOPIC_URL = "https://wx.zsxq.com/dweb2/index/topic/%s"
MAX_PUSH_COMMENTS = 5             # 一条推送里最多列几条评论
MAX_DIFF_LINES = 18               # 对比页最多列几行差异
TYPE_NAMES = {"talk": "话题", "q&a": "问答", "task": "作业", "solution": "解答"}
EVENT_ICON = {"new": "📥", "edit": "✏️", "comment": "💬", "gone": "🚫"}

MENU_TEXT = """🌍 <b>星球哨兵</b>

它盯着你加入的星球：新内容一出现就存到本地；被删、被改、评论有变动，都会告诉你。

<b>我的星球</b> — 翻备份、开推送、调节奏
<b>最近话题</b> — 所有星球的新备份，一条条点开看
<b>最近变动</b> — 这次运行里谁被改了、被删了、多了几条评论
<b>哨兵状态</b> — 盯了几个星球、存了多少、凭证还灵不灵

星球名旁边的标记：🔄 新话题自动推给你 · ✂️ 只留最近 N 条
想找某条旧话题，直接发几个字，我按标题搜给你。"""

GROUPS_EMPTY = """没能拉到你的星球列表。

多半是凭证失效了：点下面的「扫码更新凭证」，或者发 /login。
拉取失败的具体原因在服务日志里。"""


# ---------- 文本渲染 (HTML parse_mode) ----------

TAG_RE = re.compile(r"</?([A-Za-z][\w-]*)(?:\s[^>]*)?>")


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


def _plain_tags(s):
    """把已经拼好的 HTML 还原成能当纯文本发的内容 —— parse_mode 被关掉时用。"""
    return _html.unescape(TAG_RE.sub("", s or ""))


def _quote(s_html):
    """可折叠引用。入参必须已经是转义好的 HTML。"""
    return BQ_OPEN + s_html + BQ_CLOSE


def _open_tags(head):
    """head 里还没闭合的标签, 按打开顺序。"""
    stack = []
    for m in TAG_RE.finditer(head):
        tag = m.group(1).lower()
        if m.group(0).startswith("</"):
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == tag:
                    del stack[i:]
                    break
        elif not m.group(0).endswith("/>"):
            stack.append(tag)
    return stack


def _u16(s):
    """Telegram 的长度是按 UTF-16 单元算的: emoji 占 2 个, 中文占 1 个。"""
    return len(s.encode("utf-16-le")) // 2


def _clip(text, limit=None, tail="…"):
    """截到 limit 以内, 但先把标签切完整、再补上闭合标签。
    Telegram 收到半个 <a href="..."> 会拒收整条消息, 界面上就是点了没反应。"""
    limit = MAXLEN if limit is None else limit
    text = text or ""
    if _u16(text) <= limit:
        return text
    budget, cut = limit - _u16(tail), 0
    used = 0
    for ch in text:
        w = 2 if ord(ch) > 0xFFFF else 1
        if used + w > budget:
            break
        used += w
        cut += 1
    for m in TAG_RE.finditer(text):
        if m.start() >= cut:
            break
        if m.end() > cut:                 # 切点落在标签中间, 挪到标签前面
            cut = m.start()
            break
    head = text[:cut]
    amp = head.rfind("&")                 # 也别切在 &amp; 这种实体中间
    if amp != -1 and ";" not in head[amp:] and len(head) - amp <= 8:
        head = head[:amp]
    head = head.rstrip()
    return head + tail + "".join("</%s>" % t for t in reversed(_open_tags(head)))


_clamp = _clip                              # 老名字, 保留兼容


def _pack(pieces, limit=None):
    """把若干段拼成几条 ≤ 上限的消息, 一条条发; 每条都保证 HTML 是闭合的。"""
    limit = MAXLEN if limit is None else limit
    out, cur = [], ""
    for p in pieces:
        p = (p or "").strip("\n")
        if not p:
            continue
        if not cur:
            cur = p
        elif _u16(cur) + _u16(p) + 2 <= limit:
            cur += "\n\n" + p
        else:
            out.append(cur)
            cur = p
    if cur:
        out.append(cur)
    return [_clip(p, limit) for p in out]


def _ago(ts):
    d = max(0, int(time.time() - (ts or 0)))
    if d < 60:
        return "%d 秒前" % d
    if d < 3600:
        return "%d 分钟前" % (d // 60)
    if d < 86400:
        return "%d 小时前" % (d // 3600)
    return "%d 天前" % (d // 86400)


def _short(ts):
    """2026-10-14T11:07:00 → 10-14 11:07"""
    t = (ts or "").replace("T", " ")
    return t[5:16] if len(t) >= 16 else t


def _type_name(topic):
    t = (topic.get("type") or "").lower()
    return TYPE_NAMES.get(t, t or "话题")


def _author_of(topic):
    for key in ("answer", "talk", "question", "task", "solution"):
        node = topic.get(key)
        if isinstance(node, dict) and (node.get("owner") or {}).get("name"):
            return node["owner"]["name"]
    return ((topic.get("owner") or {}).get("name")) or "?"


def _counts_line(topic):
    bits = ["👍 %s" % (topic.get("likes_count") or 0),
            "💬 %s" % (topic.get("comments_count") or 0)]
    if topic.get("readers_count"):
        bits.append("👁 %s" % topic["readers_count"])
    return " · ".join(bits)


def _fmt_topic_brief(topic, gname):
    """一条话题 → 推送文本 (HTML): 标题行、提问、回答、原文地址。"""
    tid = str(topic.get("topic_id"))
    ct = (topic.get("create_time") or "")[:16].replace("T", " ")
    author = _author_of(topic)
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
    L += ["", TOPIC_URL % tid]
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


def _fmt_comment(c, idx, parent=None):
    """一条评论 (或楼中楼回复) → HTML。parent 不为空时按「回复谁」来写。"""
    owner = ((c.get("owner") or {}).get("name")) or "?"
    ct = _short(c.get("create_time"))
    tags = []
    if c.get("sticky"):
        tags.append("置顶")
    if c.get("group_owner_liked"):
        tags.append("星主赞")
    if c.get("likes_count"):
        tags.append("👍%s" % c["likes_count"])
    if parent is not None:
        to = ((c.get("repliee") or {}).get("name")) or ((parent.get("owner") or {}).get("name"))
        head = "　└ <b>%s</b>%s · %s" % (
            _html.escape(owner), (" 回复 " + _html.escape(to)) if to else "", ct)
    else:
        head = "<b>%s. %s</b> · %s" % (idx, _html.escape(owner), ct)
    if tags:
        head += " · " + " · ".join(tags)
    body = _inline_html(c.get("text") or "").strip()
    out = [head]
    if body:
        out.append(_quote(body))
    elif c.get("images"):
        out.append("（这条是图片）")
    else:
        out.append("（空）")
    if c.get("images") and body:
        out.append("🖼 附图 %d 张" % len(c["images"]))
    return "\n".join(out)


def _diff_text(diff, limit=MAX_DIFF_LINES):
    """改动对比 → 几行可读文本。diff 来自 Archive.diff_topics。"""
    if not diff:
        return "逐行看下来没有文字差别 —— 可能只改了图片、附件或标签。"
    L = []
    for d in diff:
        L.append("<b>%s</b>：新增 %d 行，删掉 %d 行" % (
            _html.escape(d["label"]), d["added"], d["removed"]))
    L.append("")
    shown = 0
    for d in diff:
        for sign, line in d["lines"]:
            if shown >= limit:
                rest = sum(len(x["lines"]) for x in diff) - shown
                L.append("…… 还有 %d 行没显示，完整内容在备份的 .md 文件里" % rest)
                return "\n".join(L)
            L.append("%s %s" % ("－" if sign == "-" else "＋", _html.escape(line)))
            shown += 1
    return "\n".join(L)


class TgBot(threading.Thread):
    """Telegram 机器人线程。共享 Sentinel 实例读星球列表/备份状态,
    并把阻塞操作丢到线程池, 保持菜单响应。"""

    KEEP_CHOICES = [0, 50, 100, 200, 500, 1000]
    # 评论提醒四档: 增多和减少是两件不同的事 (减往往是删评的前兆), 分开管才有意义
    ALERT_CHOICES = ["both", "added", "removed", "off"]
    ALERT_NAMES = {"both": "都提醒", "added": "只看新增", "removed": "只看减少", "off": "全关"}
    ALERT_HELP = {
        "both": "有人评论会告诉你谁说了什么；评论变少也会告诉你少了谁的哪一条。",
        "added": "有人评论会提醒；评论变少不出声，本地那条照样留着 —— 卡片和评论页上能看出来。",
        "removed": "只在评论变少时出声（删评、清评）；新评论照样存进本地，只是不打扰你。",
        "off": "完全不提醒，也不再追已有话题的新评论 —— 新话题备份时评论照样一起存。",
    }
    # 抓取设置的可选项 (键 → 候选值); 接口实测上限: 每轮 30 条
    OPT_CHOICES = {"poll_interval": [15, 30, 45, 60, 120, 300],
                   "page_size": [20, 30],
                   "check_interval": [300, 900, 3600, 21600, 86400, 0],
                   "recent_days": [1, 3, 7, 30],
                   "max_comments": [0, 20, 50, 100],
                   "sweep_every": [600, 1800, 3600, 10800, 21600, 0],
                   "sweep_size": [5, 10, 20, 30],
                   "quiet_hours": [[0, 0], [23, 7], [22, 8], [0, 7]],
                   "keep_media": [True, False]}
    OPT_NAMES = {"poll_interval": "抓取间隔", "page_size": "每轮条数",
                 "check_interval": "删除复查", "recent_days": "复查范围",
                 "max_comments": "评论上限", "sweep_every": "评论抽查",
                 "sweep_size": "抽查条数", "quiet_hours": "夜间静默",
                 "keep_media": "图片附件"}
    OPT_HELP = {
        "poll_interval": "多久去星球看一眼有没有新内容。调大更省请求，调小发现得更快。",
        "page_size": "一次拉多少条。星球发帖快就调大，免得一轮还没看完就翻篇了。",
        "check_interval": "老话题会慢慢掉出列表，所以隔一段时间单独问一次「它还在不在」。这是两次确认之间的最短间隔。",
        "recent_days": "只复核最近这几天的话题。更早的不再花请求去确认。",
        "max_comments": "每个话题最多存多少条评论。0 = 全部，星球再热闹也一条不漏。",
        "sweep_every": "隔多久开一批，把一批话题的评论区整段翻一遍。评论数字看不见的那部分（楼中楼被删、涨了又跌回原数）只有翻一遍才发现。",
        "sweep_size": "一批翻几条。挑的是最久没核过的话题，窗口里的迟早都轮到；一批会摊在接下来几次轮询里，一次一个请求。",
        "quiet_hours": "这段时间里不去星球抓内容（机器人照常在线），省得半夜刷请求。",
        "keep_media": "存不存图片和附件。关掉只留文字和链接，省磁盘。",
    }
    # 手动填数值: 每一项的输入单位 / 存进去要乘的系数 / 允许范围
    MANUAL = {
        "poll_interval": ("秒", 1, 5, 3600),
        "page_size": ("条", 1, 5, 30),
        "check_interval": ("分钟", 60, 0, 1440),
        "recent_days": ("天", 1, 1, 365),
        "max_comments": ("条", 1, 0, 500),
        "sweep_every": ("分钟", 60, 0, 1440),
        "sweep_size": ("条", 1, 1, 50),
    }
    UNIT_SEC = {"秒": 1, "分": 60, "分钟": 60, "时": 3600, "小时": 3600,
                "天": 86400, "条": 1, "个": 1}

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
        self.archive = Archive(getattr(sent, "out", "."))
        self._live = {}          # (gid, tid) -> 刚从服务器列表拉到、还没备份的话题
        self._finds = {}         # 搜索结果缓存: 编号 -> (关键词, 命中列表)
        self._find_seq = 0
        self._opt_change = None  # 刚改过的设置, 设置页用来回一句「改成了什么」
        self._pending = {}       # (chat_id, user_id) -> (gid, key, 时间): 正等着发一个数字过来
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
        a.add_handler(CommandHandler("cancel", self.cmd_cancel))
        # 不是命令的一句话 = 按标题搜备份
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.cmd_search))
        for pat, fn in (
                (r"^menu$", self.cb_menu), (r"^groups$", self.cb_groups),
                (r"^status$", self.cb_status), (r"^login$", self.cb_login),
                (r"^noop$", self.cb_noop),
                (r"^g:\d+$", self.cb_group),
                (r"^L:\d+:\d+$", self.cb_list),
                (r"^A:\d+$", self.cb_all),
                (r"^E:\d+$", self.cb_events),
                (r"^S:\w+:\d+$", self.cb_search_page),
                (r"^T:\d+:\d+:[LAE]:\d+$", self.cb_topic),
                (r"^F:\d+:\d+$", self.cb_full),
                (r"^C:\d+:\d+:\d+$", self.cb_comments),
                (r"^Z:\d+:\d+$", self.cb_comments_fetch),
                (r"^V:\d+:\d+:\d+$", self.cb_rev),
                (r"^U:\d+:\d+:[LAE]:\d+$", self.cb_refresh),
                (r"^P:\d+$", self.cb_latest),
                (r"^Q:\d+$", self.cb_dump_live),
                (r"^[ipcdmxyqw]:\d+$", self.cb_opt),
                (r"^Y:\d+$", self.cb_manual),
                (r"^X:\d+:[a-z_]+$", self.cb_manual_pick),
                (r"^n:\d+$", self.cb_comment_alert),
                (r"^a:\d+$", self.cb_autopush), (r"^k:\d+$", self.cb_keep),
                (r"^s:\d+$", self.cb_set)):
            a.add_handler(CallbackQueryHandler(fn, pattern=pat))

    # ---- 生命周期 ----

    async def _post_init(self, app):
        self._loop = asyncio.get_running_loop()      # 供哨兵线程跨线程投递
        try:
            await app.bot.set_my_commands([
                BotCommand("menu", "主菜单"), BotCommand("groups", "我的星球"),
                BotCommand("status", "哨兵状态"), BotCommand("login", "微信扫码更新凭证"),
                BotCommand("cancel", "取消正在等你的输入")])
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

    # ---- 哨兵 → 机器人 (跨线程投递) ----

    def _dispatch(self, coro_fn, *args):
        """把一次推送交给事件循环。返回有没有投递成功 (哨兵据此决定还要不要发普通告警)。"""
        chat = self.push_chat_id()
        if not (chat and self._loop and self.app):
            return False
        try:
            asyncio.run_coroutine_threadsafe(coro_fn(chat, *args), self._loop)
            return True
        except Exception as e:
            log("[!] 推送投递失败: %s" % str(e)[:120])
            return False

    def push_text(self, text):
        """哨兵 (同步线程) 调用: 把一条 HTML 文本投递进事件循环。"""
        chat = self.push_chat_id()
        if not (chat and self._loop and self.app):
            return
        try:
            asyncio.run_coroutine_threadsafe(self._send(chat, text), self._loop)
        except Exception as e:
            log("[!] 推送投递失败: %s" % str(e)[:120])

    def push_topic(self, topic, gname, gid=None):
        """新话题推全文 + 图, 附「看评论 / 看全文」按钮。"""
        return self._dispatch(self._send_topic, topic, gname,
                              gid or self._find_gid(topic))

    def push_comment_change(self, gid, tid, title, gname, total,
                            added, removed, complete=True):
        """评论数变了: 把新增/消失的评论连同跳转按钮推过去。返回是否已投递。"""
        return self._dispatch(self._send_comment_change, gid, tid, title, gname,
                              total, added, removed, complete)

    def push_edit_change(self, gid, tid, title, gname, rev_n, diff):
        """话题被编辑: 推一份改动摘要。"""
        return self._dispatch(self._send_edit_change, gid, tid, title, gname, rev_n, diff)

    def push_deleted(self, gid, tid, title, gname):
        """话题被删: 推一条带「看本地快照」按钮的消息。"""
        return self._dispatch(self._send_deleted, gid, tid, title, gname)

    def _find_gid(self, topic):
        tid = str(topic.get("topic_id"))
        for key in snap(self.s.state):
            g, t = key.split("/", 1) if "/" in key else ("", "")
            if t == tid:
                return g
        g = (topic.get("group") or {}).get("group_id")
        return str(g) if g else ""

    # ---- 发送 ----

    async def _send(self, chat_id, text, kb=None):
        body = _clip(text)
        try:
            await self.app.bot.send_message(chat_id, body, parse_mode=ParseMode.HTML,
                                            link_preview_options=NOPREVIEW, reply_markup=kb)
        except telegram.error.BadRequest as e:
            # HTML 被拒(半截标签、没转义的 &、星球名里带 <): 退回纯文本再发一次,
            # 总比什么都不显示强 —— 光标在页面上, 用户看得见内容
            log("[!] HTML 被拒, 退回纯文本重发: %s" % str(e)[:120])
            try:
                await self.app.bot.send_message(chat_id, _plain_tags(body), parse_mode=None,
                                                link_preview_options=NOPREVIEW, reply_markup=kb)
            except Exception as e2:
                log("[!] Telegram 发送失败: %s" % str(e2)[:150])
        except Exception as e:
            log("[!] Telegram 发送失败: %s" % str(e)[:150])

    async def _send_topic(self, chat_id, topic, gname, gid=None):
        tid = str(topic.get("topic_id"))
        kb = None
        if gid:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("💬 看评论", callback_data="C:%s:%s:0" % (gid, tid)),
                InlineKeyboardButton("📄 看全文", callback_data="F:%s:%s" % (gid, tid))]])
        await self._send(chat_id, _fmt_topic_brief(topic, gname), kb)
        await self._send_images(chat_id, _topic_image_groups(topic))

    async def _send_comment_change(self, chat_id, gid, tid, title, gname,
                                   total, added, removed, complete):
        title = (title or "(无标题)")[:60]
        if added and removed:
            what = "评论多了 %d 条、少了 %d 条" % (len(added), len(removed))
        elif added:
            what = "多了 %d 条评论" % len(added)
        elif removed:
            what = "少了 %d 条评论" % len(removed)
        else:
            what = "的评论有变化"
        head = "💬 「%s」%s" % (_html.escape(title), what)
        L = [head + "（现在共 %d 条）" % total, "星球：%s" % _html.escape(gname), ""]
        if not added and not removed:
            L.append("没比出具体是哪几条 —— 多半是被「评论上限」挡住了，"
                     "调成「全部」下次就能看到。")
        for lead, group in (("新来的：", added),
                            ("服务器上不再出现、本地还留着的：", removed)):
            if not group:
                continue
            L.append(lead)
            for i, (c, parent) in enumerate(group[:MAX_PUSH_COMMENTS], 1):
                who = ((c.get("owner") or {}).get("name")) or "?"
                line = "%d. <b>%s</b> · %s" % (i, _html.escape(who), _short(c.get("create_time")))
                if parent is not None:
                    to = ((c.get("repliee") or {}).get("name")) or ((parent.get("owner") or {}).get("name") or "?")
                    line += " · 回复 %s" % _html.escape(to)
                body = _inline_html(c.get("text") or "").strip()
                L.append(line)
                L.append(_quote(_clip(body, 300)) if body else "（图片或空）")
            if len(group) > MAX_PUSH_COMMENTS:
                L.append("…… 另外还有 %d 条，点下面看全部。" % (len(group) - MAX_PUSH_COMMENTS))
            L.append("")                       # 两组之间空一行, 免得读成一串
        if not complete:
            if L[-1] != "":
                L.append("")
            L.append("⚠️ 这条话题的评论超过了「评论上限」，只比了前面那部分，"
                     "可能还有没看到的。在抓取设置里把评论调成「全部」就齐了。")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("💬 看这些评论", callback_data="C:%s:%s:0" % (gid, tid)),
            InlineKeyboardButton("📄 看话题", callback_data="T:%s:%s:L:1" % (gid, tid))]])
        await self._send(chat_id, "\n".join(L), kb)

    async def _send_edit_change(self, chat_id, gid, tid, title, gname, rev_n, diff):
        L = ["✏️ 「%s」被改过" % _html.escape((title or "(无标题)")[:60]),
             "星球：%s · 第 %d 次改动" % (_html.escape(gname), rev_n), ""]
        L.append(_diff_text(diff, limit=8))
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✏️ 逐行对比", callback_data="V:%s:%s:%d" % (gid, tid, rev_n)),
            InlineKeyboardButton("📄 看话题", callback_data="T:%s:%s:L:1" % (gid, tid))]])
        await self._send(chat_id, "\n".join(L), kb)

    async def _send_deleted(self, chat_id, gid, tid, title, gname):
        L = ["🚫 「%s」在服务器上被删了" % _html.escape((title or "(无标题)")[:60]),
             "星球：%s" % _html.escape(gname),
             "", "本地存着删除之前的完整快照，正文和评论都还在。"]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📄 看本地快照", callback_data="T:%s:%s:L:1" % (gid, tid)),
            InlineKeyboardButton("🗂 备份清单", callback_data="L:%s:1" % gid)]])
        await self._send(chat_id, "\n".join(L), kb)

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
        msg = getattr(query, "message", None)
        if msg is None:
            return
        try:
            await query.edit_message_text(_clip(text), parse_mode=ParseMode.HTML,
                                          link_preview_options=NOPREVIEW, reply_markup=kb)
        except telegram.error.BadRequest as e:
            if "not modified" not in str(e).lower():
                await self._send(msg.chat_id, text, kb)
        except Exception:
            await self._send(msg.chat_id, text, kb)

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
                if self.admins:
                    msg = ("⛔ 你不在允许名单里 (你的 ID: %s)。\n"
                           "把这个数字加进 watch.json 的 bot_users, 重启即可。" % uid)
                else:
                    msg = ("⛔ 没配 bot_users 的时候, 机器人只在私聊里干活, 群里一律不理。\n"
                           "想在这里用, 就把你的 ID 填进 watch.json 的 bot_users。")
                await self._send(update.effective_chat.id, msg)
            raise ApplicationHandlerStop
        self.remember_chat(update)
        # 「等你发数字」这个状态很容易把人卡住: 点别的按钮、发别的命令, 都当作已经不填了。
        # /cancel 例外, 它自己会回一句「好, 不填了」。
        msg = getattr(update, "message", None)
        q = getattr(update, "callback_query", None)
        chat = getattr(msg, "chat_id", None) or getattr(getattr(q, "message", None), "chat_id", None)
        user = update.effective_user.id if update.effective_user else 0
        text = getattr(msg, "text", "") or ""
        if chat and (q is not None or (text.startswith("/") and not text.startswith("/cancel"))):
            self._pending.pop((chat, user), None)
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
            # 星球上设的抓取节奏和保留条数也要落盘, 否则重启即丢
            keep = {k: v for k, v in (self.s.cfg.get("group_keep") or {}).items() if v}
            opts = {k: v for k, v in (self.s.cfg.get("group_opts") or {}).items() if v}
            for key, val in (("group_keep", keep), ("group_opts", opts)):
                if val:
                    data[key] = val
                else:
                    data.pop(key, None)
            save_json(p, data, indent=1)
        except Exception as e:
            log("[!] 机器人偏好保存失败: %s" % e)

    # ---- 键盘 ----

    def kb_menu(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🌍 我的星球", callback_data="groups")],
            [InlineKeyboardButton("🗂 最近话题", callback_data="A:1"),
             InlineKeyboardButton("📋 最近变动", callback_data="E:1")],
            [InlineKeyboardButton("📊 哨兵状态", callback_data="status")],
            [InlineKeyboardButton("🔐 扫码更新凭证", callback_data="login")]])

    def kb_groups(self):
        if not self.s.gnames:                    # 名单还没拉上来, 先给回菜单, 别给一排空按钮
            return self.kb_menu()
        rows = []
        for gid in sorted(self.s.gnames, key=lambda g: self.s.gnames[g]):
            marks = ""
            if self.autopush.get(str(gid)):
                marks += " 🔄"
            if (self.s.cfg.get("group_keep") or {}).get(str(gid)):
                marks += " ✂️"
            rows.append([InlineKeyboardButton(self.s.gnames[gid] + marks,
                                              callback_data="g:" + gid)])
        rows.append([InlineKeyboardButton("🔄=新话题自动推送  ✂️=只留最近", callback_data="noop")])
        return InlineKeyboardMarkup(rows)

    def kb_group(self, gid):
        g = str(gid)
        on = bool(self.autopush.get(g))
        keep = int((self.s.cfg.get("group_keep") or {}).get(g) or 0)
        alert = self.s.opt(g, "comment_alert")
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🗂 备份清单", callback_data="L:%s:1" % g)],
            [InlineKeyboardButton("📥 拉最新 10 条", callback_data="P:%s" % g)],
            [InlineKeyboardButton("💬 评论提醒 · " + self.ALERT_NAMES.get(alert, alert),
                                  callback_data="n:" + g)],
            [InlineKeyboardButton(("🔔 自动推送 · 开" if on else "🔕 自动推送 · 关"),
                                  callback_data="a:" + g)],
            [InlineKeyboardButton(("✂ 保留 · 只留 %d 条" % keep) if keep else "✂ 保留 · 不限量",
                                  callback_data="k:" + g)],
            [InlineKeyboardButton("⚙ 抓取设置", callback_data="s:" + g)],
            [InlineKeyboardButton("◀ 我的星球", callback_data="groups")]])

    def kb_set(self, gid):
        """抓取设置页: 每一行点一下就换到下一个候选值, 当前值直接写在按钮上。"""
        g = str(gid)
        rows = []
        for key, prefix in (("poll_interval", "i"), ("page_size", "p"),
                            ("check_interval", "c"), ("recent_days", "d"),
                            ("max_comments", "m"), ("sweep_every", "x"),
                            ("sweep_size", "y"), ("quiet_hours", "q"),
                            ("keep_media", "w")):
            rows.append([InlineKeyboardButton(
                "%s · %s" % (self.OPT_NAMES[key], self._opt_value(key, self.s.opt(g, key))),
                callback_data="%s:%s" % (prefix, g))])
        rows.append([InlineKeyboardButton("✏️ 手动填数值", callback_data="Y:" + g)])
        rows.append([InlineKeyboardButton("◀ 返回该星球", callback_data="g:" + g)])
        return InlineKeyboardMarkup(rows)

    def kb_manual(self, gid):
        """手动填数值时先选一项; 单位写在按钮上, 免得用户不知道填的是什么。"""
        g = str(gid)
        rows = []
        for key in ("poll_interval", "page_size", "check_interval", "recent_days",
                    "max_comments", "sweep_every", "sweep_size"):
            unit = self.MANUAL[key][0]
            rows.append([InlineKeyboardButton(
                "%s（%s）" % (self.OPT_NAMES[key], unit),
                callback_data="X:%s:%s" % (g, key))])
        rows.append([InlineKeyboardButton("◀ 返回设置", callback_data="s:" + g)])
        return InlineKeyboardMarkup(rows)

    @staticmethod
    def _fmt_sec(v, zero="关闭"):
        try:
            n = int(v)
        except (TypeError, ValueError):
            return "?"
        if n <= 0:
            return zero
        if n < 60:
            return "%d 秒" % n
        if n < 3600:
            m, s = divmod(n, 60)
            return ("%d 分钟" % m) if not s else ("%d 分 %d 秒" % (m, s))
        if n < 86400:
            h, rest = divmod(n, 3600)
            m = rest // 60
            return ("%d 小时" % h) if not m else ("%d 小时 %d 分" % (h, m))
        d, rest = divmod(n, 86400)
        h = rest // 3600
        return ("%d 天" % d) if not h else ("%d 天 %d 小时" % (d, h))

    @staticmethod
    def _fmt_quiet(v):
        q = (list(v or []) + [0, 0])[:2]
        return "关闭" if q[0] == q[1] else "%d 点–%d 点" % (q[0], q[1])

    @classmethod
    def _read_num(cls, text, unit):
        """把用户发来的话读成一个数, 单位是 unit:
        '90' / '90 秒' / '2分钟' / '1.5小时' / '7 天' 都认; 读不出来给 None。"""
        s = (text or "").strip().replace(" ", "").replace("，", "")
        if not s:
            return None
        mult = cls.UNIT_SEC.get(unit, 1)
        for name in sorted(cls.UNIT_SEC, key=len, reverse=True):
            if s.endswith(name) and len(s) > len(name):
                s, mult = s[:-len(name)], cls.UNIT_SEC[name]
                break
        try:
            n = float(s)
        except ValueError:
            return None
        # nan/inf/天文数字都不接: 后面乘系数、转 int 会炸, 而且也没意义
        if n != n or n < 0 or n > 1e9:
            return None
        return n * mult / cls.UNIT_SEC.get(unit, 1)

    def manual_value(self, key, text):
        """把一句话变成这个设置该存的值。读不出来就返回 (None, 原因)。"""
        unit, factor, lo, hi = self.MANUAL[key]
        n = self._read_num(text, unit)
        if n is None:
            return None, "「%s」不像一个数字" % (text or "").strip()[:20]
        value = int(round(n * factor))
        if not (lo * factor <= value <= hi * factor):
            return None, "只能在 %s–%s %s 之间" % (lo, hi, unit)
        return value, ""

    def pending_get(self, chat_id, user_id):
        """取出等着填的那一项。超过 5 分钟就当没这回事 —— 隔了很久的一句话
        多半是想搜索或者干别的, 不能当成数字吃掉。"""
        item = self._pending.get((chat_id, user_id))
        if not item:
            return None
        gid, key, ts = item
        if time.time() - ts > 300:
            self._pending.pop((chat_id, user_id), None)
            return None
        return (gid, key)

    def pending_put(self, chat_id, user_id, gid, key):
        self._pending[(chat_id, user_id)] = (gid, key, time.time())

    async def apply_manual(self, update, pending):
        """用户发来的那句话当成数字用; 读不出来就再问一次, 别把他的话丢了。"""
        chat = update.effective_chat.id
        who = update.effective_user.id if update.effective_user else 0
        gid, key = pending
        value, why = self.manual_value(key, update.message.text or "")
        if value is None:
            self.pending_put(chat, who, gid, key)     # 还等着, 下次再说
            await self._send(chat, "\n".join([
                "这个我没用上：%s。" % _html.escape(why),
                "「%s」要一个%s数，再发一次就行；不想填了就发 /cancel。"
                % (self.OPT_NAMES[key], self.MANUAL[key][0])]))
            return
        self.s.set_opt(gid, key, value)
        self._opt_change = (key, value)
        self.save_pref()
        await self._send(chat, self.set_text(gid), self.kb_set(gid))

    def _opt_value(self, key, value):
        """把设置值说成人话。按钮和设置页都用它。"""
        if key == "poll_interval":
            return self._fmt_sec(value)
        if key == "page_size":
            return "%d 条" % int(value or 20)
        if key == "check_interval":
            return self._fmt_sec(value, zero="关闭")
        if key == "recent_days":
            return "最近 %d 天" % int(value or 0)
        if key == "max_comments":
            return "全部" if not int(value or 0) else "%d 条" % int(value)
        if key == "sweep_every":
            return "关" if not int(value or 0) else self._fmt_sec(value)
        if key == "sweep_size":
            return "每批 %d 条" % int(value or 20)
        if key == "quiet_hours":
            return self._fmt_quiet(value)
        if key == "keep_media":
            return "下载" if value else "跳过"
        return str(value)

    def _opt_meaning(self, key, value, gid=None):
        """刚改成这个值, 意味着什么。设置页改完回一句。"""
        if key == "poll_interval":
            return "以后去星球的间隔是 %s，新内容最晚这么久之后被发现。" % self._fmt_sec(value)
        if key == "page_size":
            return "每轮拉最新 %d 条，一次能盖住这么多。" % int(value or 20)
        if key == "check_interval":
            return ("掉出列表的话题，两次确认之间至少隔 %s。" % self._fmt_sec(value)
                    if int(value or 0) else "不再单独确认删除，只靠列表里出现与否判断。")
        if key == "recent_days":
            return "只复核最近 %d 天内的话题。" % int(value or 0)
        if key == "max_comments":
            return ("每个话题的评论全抓，一条不漏。" if not int(value or 0)
                    else "每个话题最多存 %d 条评论，再多就不抓了。" % int(value))
        if key == "sweep_every":
            return ("不再翻评论区，评论只跟着数字走（楼中楼被删就看不见了）。"
                    if not int(value or 0) else
                    "一批翻完、且距上一次开批满 %s，才开下一批。"
                    % self._fmt_sec(value))
        if key == "sweep_size":
            return ("一批翻 %d 条 —— 摊在接下来 %d 次轮询里，一次一个请求。先挑最久没核过的。"
                    % (int(value or 20), int(value or 20)))
        if key == "quiet_hours":
            return ("夜里也照常抓，不再静默。" if self._fmt_quiet(value) == "关闭"
                    else "这段时间（%s）不去星球抓内容，机器人照常在线。" % self._fmt_quiet(value))
        if key == "keep_media":
            return "图片和附件都会下到本地。" if value else "只留文字和链接，不再下图。"
        return ""

    # ---- 命令 ----

    async def cmd_menu(self, update, context):
        await self._send(update.effective_chat.id, MENU_TEXT, self.kb_menu())

    async def cmd_groups(self, update, context):
        await self._send(update.effective_chat.id,
                         await self.groups_text(), self.kb_groups())

    async def cmd_status(self, update, context):
        await self._send(update.effective_chat.id,
                         await asyncio.to_thread(self.status_text))

    async def cmd_login(self, update, context):
        context.application.create_task(self.do_login(update.effective_chat.id))

    async def cmd_cancel(self, update, context):
        chat = update.effective_chat.id
        who = update.effective_user.id if update.effective_user else 0
        had = self._pending.pop((chat, who), None)
        if had:
            await self._send(chat, "好，这次不填了，设置还是原来的样子。发 /menu 回主菜单。")
        else:
            await self._send(chat, "现在没有等你填的数值。发 /menu 回主菜单。")

    async def cmd_search(self, update, context):
        word = (update.message.text or "").strip()
        chat = update.effective_chat.id
        who = update.effective_user.id if update.effective_user else 0
        pending = self.pending_get(chat, who)
        if pending:                      # 刚才在等一个数字, 这句话就当成那个数字
            self._pending.pop((chat, who), None)
            await self.apply_manual(update, pending)
            return
        if not word:
            return
        await self._send(chat, "正在翻本地备份的标题找「%s」…" % _html.escape(word[:20]))
        hits = await asyncio.to_thread(self.archive.search, self.s.state, word)
        if not hits:
            await self._send(chat, "没找到标题里带「%s」的备份。\n\n"
                                   "（只按标题搜，不搜正文。发 /menu 回主菜单。）"
                             % _html.escape(word[:20]))
            return
        token = self._remember_find(word, hits)
        await self._send(chat, *self._list_view(hits, 1, "🔎 标题含「%s」" % _html.escape(word[:20]),
                                               origin="A", gid=None, token=token, with_group=True))

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
        await self._show(q, await self.groups_text(), self.kb_groups())

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
        await self._show(q, await asyncio.to_thread(self.group_text, gid), self.kb_group(gid))

    async def cb_list(self, update, context):
        q = update.callback_query
        await q.answer()
        _, gid, page = q.data.split(":")
        text, kb = await asyncio.to_thread(self._archive_page, gid, int(page))
        await self._show(q, text, kb)

    async def cb_all(self, update, context):
        q = update.callback_query
        await q.answer()
        page = int(q.data.split(":", 1)[1])
        text, kb = await asyncio.to_thread(self._all_page, page)
        await self._show(q, text, kb)

    async def cb_events(self, update, context):
        q = update.callback_query
        await q.answer()
        text, kb = self._events_page(int(q.data.split(":", 1)[1]))
        await self._show(q, text, kb)

    async def cb_search_page(self, update, context):
        q = update.callback_query
        await q.answer()
        _, token, page = q.data.split(":")
        hit = self._finds.get(token)
        if not hit:
            await self._show(q, "这次搜索的结果已经过期了，重新发一次关键词吧。", self.kb_menu())
            return
        word, hits = hit
        await self._show(q, *self._list_view(
            hits, int(page), "🔎 标题含「%s」" % _html.escape(word), origin="A",
            gid=None, token=token, with_group=True))

    async def cb_topic(self, update, context):
        q = update.callback_query
        await q.answer()
        _, gid, tid, src, page = q.data.split(":")
        text, kb = await asyncio.to_thread(self._card_view, gid, tid, src, int(page))
        await self._show(q, text, kb)

    async def cb_full(self, update, context):
        q = update.callback_query
        await q.answer("正在发…")
        _, gid, tid = q.data.split(":")
        parts = await asyncio.to_thread(self._full_texts, gid, tid)
        for p in parts:
            await self._send(q.message.chat_id, p)
        await self._send_images(q.message.chat_id, await asyncio.to_thread(
            self._topic_images, gid, tid))

    async def cb_comments(self, update, context):
        q = update.callback_query
        await q.answer()
        _, gid, tid, page = q.data.split(":")
        text, kb = await asyncio.to_thread(self._comments_view, gid, tid, int(page))
        await self._show(q, text, kb)

    async def cb_comments_fetch(self, update, context):
        q = update.callback_query
        await q.answer("正在从星球拉评论…")
        _, gid, tid = q.data.split(":")
        text, kb = await self._fetch_comments_and_show(gid, tid)
        await self._show(q, text, kb)

    async def cb_rev(self, update, context):
        q = update.callback_query
        await q.answer()
        _, gid, tid, n = q.data.split(":")
        text, kb = await asyncio.to_thread(self._rev_view, gid, tid, int(n))
        await self._show(q, text, kb)

    async def cb_refresh(self, update, context):
        q = update.callback_query
        await q.answer("正在问星球…")
        _, gid, tid, src, page = q.data.split(":")
        cli = self.s.cli
        resp = await asyncio.to_thread(cli.req, "/v2/topics/%s" % tid)
        if resp.get("succeeded"):
            topic = (resp.get("resp_data") or {}).get("topic") or {}
            if topic:
                self._remember_live(gid, [topic])
        text, kb = await asyncio.to_thread(self._card_view, gid, tid, src, int(page), resp)
        await self._show(q, text, kb)

    async def cb_latest(self, update, context):
        q = update.callback_query
        await q.answer("正在从服务器拉…")
        await self.show_latest(q.message.chat_id, q.data.split(":", 1)[1])

    async def cb_dump_live(self, update, context):
        q = update.callback_query
        await q.answer("正在发全文…")
        await self.dump_live(q.message.chat_id, q.data.split(":", 1)[1])

    async def cb_autopush(self, update, context):
        q = update.callback_query
        await q.answer()
        gid = q.data.split(":", 1)[1]
        await self._show(q, self.toggle_autopush(gid), self.kb_group(gid))

    async def cb_keep(self, update, context):
        q = update.callback_query
        await q.answer()
        gid = q.data.split(":", 1)[1]
        # 清理文件, 别卡住循环
        msg = await asyncio.to_thread(self.cycle_keep, gid)
        await self._show(q, msg, self.kb_group(gid))

    async def cb_comment_alert(self, update, context):
        q = update.callback_query
        await q.answer()
        gid = q.data.split(":", 1)[1]
        mode = self.cycle_alert(gid)
        gname = self.s.gnames.get(gid, gid)
        how = ("想看新的也不难：在话题的评论页点「从星球拉最新评论」。"
               if mode == "off" else "改完立刻生效。再点一下换下一档。")
        await self._show(q, "\n".join([
            "「%s」评论提醒：%s" % (_html.escape(gname), self.ALERT_NAMES[mode]),
            "",
            self.ALERT_HELP[mode],
            how,
        ]), self.kb_group(gid))

    async def cb_set(self, update, context):
        q = update.callback_query
        await q.answer()
        gid = q.data.split(":", 1)[1]
        await self._show(q, self.set_text(gid), self.kb_set(gid))

    async def cb_opt(self, update, context):
        q = update.callback_query
        await q.answer()
        key, gid = q.data.split(":", 1)
        name = {"i": "poll_interval", "p": "page_size", "c": "check_interval",
                "d": "recent_days", "m": "max_comments", "x": "sweep_every",
                "y": "sweep_size", "q": "quiet_hours",
                "w": "keep_media"}[key]
        self.cycle_opt(gid, name)
        await self._show(q, self.set_text(gid), self.kb_set(gid))

    async def cb_manual(self, update, context):
        q = update.callback_query
        await q.answer()
        gid = q.data.split(":", 1)[1]
        await self._show(q, "\n".join([
            "✏️ <b>手动填数值</b>",
            "",
            "点一项，然后直接发一个数字给我，单位写在括号里。",
            "带单位也行，比如「2分钟」「45秒」。"]), self.kb_manual(gid))

    async def cb_manual_pick(self, update, context):
        q = update.callback_query
        await q.answer()
        _, gid, key = q.data.split(":")
        self.pending_put(q.message.chat_id, q.from_user.id, gid, key)
        unit, _factor, lo, hi = self.MANUAL[key]
        ex = {"秒": "45", "条": "20", "分钟": "30", "天": "7"}[unit]
        tip = "发一个数字过来，单位是%s，比如「%s」。范围 %s–%s。" % (unit, ex, lo, hi)
        if key in ("check_interval", "sweep_every"):
            tip += "填 0 就是关掉。"
        elif key == "max_comments":
            tip += "填 0 就是全抓，不限条数。"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀ 返回设置", callback_data="s:" + gid)]])
        await self._show(q, "\n".join([
            "✏️ <b>%s</b>：现在是 %s" % (self.OPT_NAMES[key], self._opt_value(key, self.s.opt(gid, key))),
            "", tip,
            "改主意了就发 /cancel。"]), kb)

    # ---- 页面 ----

    def group_text(self, gid):
        s, g = self.s, str(gid)
        es = self.archive.entries(s.state, g)
        live = sum(1 for e in es if not e["deleted"])
        gone = len(es) - live
        last = (getattr(s, "last_poll", {}) or {}).get(g)
        keep = int((s.cfg.get("group_keep") or {}).get(g) or 0)
        push = bool(self.autopush.get(g))
        alert = s.opt(g, "comment_alert")
        L = ["🌍 <b>%s</b>" % _html.escape(s.gnames.get(g, g)), ""]
        L.append("已备份 %d 条话题%s" % (live, ("，另外 %d 条已被服务器删掉" % gone) if gone else ""))
        L.append("上次检查：%s" % (_ago(last) if last else "还没查过，等下一轮"))
        L.append("节奏：%s" % s.describe(g, with_name=False))
        L.append("")
        L.append("自动推送：%s（%s）" % (
            "开" if push else "关",
            "新话题直接发到这个聊天" if push else "新话题只存本地，不打扰你"))
        L.append("评论提醒：%s（%s）" % (
            self.ALERT_NAMES.get(alert, alert),
            "点这一行换下一档" if alert != "off" else "不提醒，也不再追新评论"))
        L.append("保留：%s" % ("不限量" if not keep else "只留最近 %d 条，旧的自动清掉" % keep))
        L.append("")
        L.append("点「备份清单」翻任意一条；点「拉最新」直接从服务器看现在有什么。")
        return "\n".join(L)

    def _archive_page(self, gid, page):
        es = self.archive.entries(self.s.state, gid)
        head = "🗂 「%s」的备份" % _html.escape(self.s.gnames.get(str(gid), str(gid)))
        return self._list_view(es, page, head, origin="L", gid=gid)

    def _all_page(self, page):
        es = self.archive.entries(self.s.state)
        return self._list_view(es, page, "🗂 所有星球的最新备份",
                               origin="A", gid=None, with_group=True)

    def _list_view(self, entries, page, head, origin, gid=None, token=None,
                   with_group=False, note=None):
        total = len(entries)
        pages = max(1, (total + LIST_PAGE - 1) // LIST_PAGE)
        page = max(1, min(int(page or 1), pages))
        chunk = entries[(page - 1) * LIST_PAGE:page * LIST_PAGE]
        lines = [head, ""]
        if note:
            lines += [note, ""]
        if not entries:
            lines.append("这里还没有备份。让哨兵跑一轮，或者回星球页点「拉最新 10 条」看看现在有什么。")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton(
                "◀ 回星球" if gid else "◀ 主菜单",
                callback_data=("g:" + str(gid)) if gid else "menu")]])
            return "\n".join(lines), kb
        lines.append("共 %d 条 · 第 %d/%d 页 · 新的在上" % (total, page, pages))
        lines.append("点一条进去：正文、全部评论、改动对比都在里面。")
        rows = []
        for e in chunk:
            rows.append([InlineKeyboardButton(self._entry_label(e, with_group),
                                              callback_data=self._topic_cb(e, origin, page, token))])
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("◀ 上一页", callback_data=self._page_cb(origin, gid, page - 1, token)))
        nav.append(InlineKeyboardButton("%d/%d" % (page, pages), callback_data="noop"))
        if page < pages:
            nav.append(InlineKeyboardButton("下一页 ▶", callback_data=self._page_cb(origin, gid, page + 1, token)))
        rows.append(nav)
        rows.append([InlineKeyboardButton(
            "◀ 回星球" if gid else "◀ 主菜单",
            callback_data=("g:" + str(gid)) if gid else "menu")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    @staticmethod
    def _page_cb(origin, gid, page, token):
        if token:
            return "S:%s:%d" % (token, page)
        if origin == "L":
            return "L:%s:%d" % (gid, page)
        return "%s:%d" % (origin, page)

    @staticmethod
    def _topic_cb(e, origin, page, token):
        return "T:%s:%s:%s:%d" % (e["gid"], e["tid"], origin, page)

    def _entry_label(self, e, with_group=False):
        title = e.get("title") or self.archive.title(e["gid"], e["tid"], e.get("rec"))
        title = (title or "(无标题)").replace("\n", " ").strip()
        marks = ""
        if e["deleted"]:
            marks += " 🚫"
        if e["revs"]:
            marks += " ✏️"
        if e["cc"]:
            marks += " 💬%d" % e["cc"]
        day = (e["time"] or "")[5:10] or "??-??"
        if with_group:
            gname = self.s.gnames.get(e["gid"], e["gid"])
            return "%s %s %s%s" % (day, gname[:6], title[:12], marks)
        return "%s %s%s" % (day, title[:16], marks)

    def _events_page(self, page):
        evs = snap_list(getattr(self.s, "events", None))
        evs.reverse()                                   # 新到旧
        rows, lines = [], ["📋 <b>最近变动</b>", ""]
        if not evs:
            lines.append("这次运行还没记下什么。新话题、被改、被删、评论变动都会出现在这里。")
            return "\n".join(lines), InlineKeyboardMarkup([[
                InlineKeyboardButton("◀ 主菜单", callback_data="menu")]])
        size = LIST_PAGE + 2
        pages = max(1, (len(evs) + size - 1) // size)
        page = max(1, min(int(page or 1), pages))
        chunk = evs[(page - 1) * size:page * size]
        lines.append("本次运行记下 %d 件 · 第 %d/%d 页 · 新的在上" % (len(evs), page, pages))
        for ev in chunk:
            icon = EVENT_ICON.get(ev.get("kind"), "·")
            gname = self.s.gnames.get(str(ev.get("gid")), str(ev.get("gid")))
            title = (ev.get("title") or "(无标题)")[:12]
            rows.append([InlineKeyboardButton(
                "%s %s %s" % (icon, gname[:5], title),
                callback_data="T:%s:%s:L:1" % (ev.get("gid"), ev.get("tid")))])
            if ev.get("note"):
                lines.append("%s %s · %s：%s" % (icon, _short(_ts_text(ev.get("t"))),
                                                 _html.escape(gname), _html.escape(ev["note"])))
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("◀ 上一页", callback_data="E:%d" % (page - 1)))
        nav.append(InlineKeyboardButton("%d/%d" % (page, pages), callback_data="noop"))
        if page < pages:
            nav.append(InlineKeyboardButton("下一页 ▶", callback_data="E:%d" % (page + 1)))
        rows.append(nav)
        rows.append([InlineKeyboardButton("◀ 主菜单", callback_data="menu")])
        return "\n".join(lines), InlineKeyboardMarkup(rows)

    def _card_view(self, gid, tid, src="L", page=1, resp=None):
        """一条话题的卡片: 元信息 + 本地存了什么 + 四个入口。"""
        raw = self.archive.load(gid, tid)
        rec = self.s.state.get("%s/%s" % (gid, tid)) or {}
        topic = raw.get("topic") or {}
        live = self._live.get((str(gid), str(tid))) or {}
        if not topic and live:
            topic = live
        title = (_plain(topic.get("title") or "").strip()
                 or self.archive.title(gid, tid, rec))
        gname = self.s.gnames.get(str(gid), str(gid))
        stored = Archive.comments_of(raw)
        have = Archive.comment_total(stored)      # 主评论 + 楼中楼, 跟接口的 comments_count 同口径
        reps = Archive.reply_total(stored)
        revs = self.archive.rev_numbers(gid, tid)
        L = ["💬 <b>%s</b>" % _html.escape(title[:80]), ""]
        if not topic:
            L += ["星球：%s" % _html.escape(gname),
                  "", "这条的存档文件不在了（可能被「保留条数」清掉过）。",
                  "点下面的「跟服务器核对一次」，我当场去问一下。"]
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 跟服务器核对一次", callback_data="U:%s:%s:%s:%d" % (gid, tid, src, page))],
                [InlineKeyboardButton(self._back_label(src), callback_data=self._back_cb(gid, src, page))]])
            return "\n".join(L), kb
        L.append("星球：%s" % _html.escape(gname))
        L.append("%s · %s · %s" % (_html.escape(_author_of(topic)),
                                   _short(topic.get("create_time")), _type_name(topic)))
        L.append(_counts_line(topic))
        badges = []
        if topic.get("digested"):
            badges.append("★ 精华")
        if topic.get("sticky"):
            badges.append("📌 置顶")
        if rec.get("deleted"):
            badges.append("🚫 服务器上已删除")
        if revs:
            badges.append("✏️ 改过 %d 次" % len(revs))
        if not raw:
            badges.append("🆕 还没备份")
        if badges:
            L.append(" ".join(badges))
        L.append("")
        if raw:
            when = (raw.get("_fetched_at") or "")[:16]
            bits = ["本地存了 %d 条评论" % have]
            if reps:
                bits.append("含 %d 条楼中楼" % reps)
            if when:
                bits.append("存档于 %s" % when)
            L.append(" · ".join(bits))
            if revs:
                L.append("改动快照：%d 份，逐行对比在这里看" % len(revs))
        else:
            L.append("这条还没备份到本地，下面看到的是刚从服务器拉来的内容。")
        if rec.get("cc") and have < int(rec["cc"]):
            L.append("上次看到服务器有 %d 条评论（含楼中楼）—— 本地不全，点进评论页拉一次就补齐。"
                     % int(rec["cc"]))
        elif rec.get("cc") and have > int(rec["cc"]):
            L.append("上次看到服务器只有 %d 条 —— 那边多半删过，本地这份还留着。"
                     % int(rec["cc"]))
        if resp is not None:
            L.append("")
            if resp.get("succeeded"):
                now = ((resp.get("resp_data") or {}).get("topic")) or {}
                cc_now = int(now.get("comments_count") or 0)
                mt = (now.get("modify_time") or "")[:16].replace("T", " ")
                L.append("刚问过服务器：%d 条评论 · %s" % (
                    cc_now, ("最后改于 " + mt) if mt else "没被改过"))
                if cc_now != int(rec.get("cc") or have):
                    L.append("（跟本地记的不一样，下一轮会再对一次）")
            elif topic_gone(resp):
                L.append("刚问过服务器：这条话题不存在或已被删除。本地快照还在。")
            else:
                L.append("没问成：%s" % _html.escape(str(resp.get("error") or "请求失败")[:80]))
        rows = [[InlineKeyboardButton("📄 看全文", callback_data="F:%s:%s" % (gid, tid)),
                 InlineKeyboardButton(("💬 评论 %d" % have) if have else "💬 评论",
                                      callback_data="C:%s:%s:1" % (gid, tid))]]
        if revs:
            rows.append([InlineKeyboardButton("✏️ 改动逐行对比（%d 次）" % len(revs),
                                              callback_data="V:%s:%s:0" % (gid, tid))])
        rows.append([InlineKeyboardButton("🔄 跟服务器核对一次",
                                          callback_data="U:%s:%s:%s:%d" % (gid, tid, src, page))])
        rows.append([InlineKeyboardButton(self._back_label(src),
                                          callback_data=self._back_cb(gid, src, page))])
        return "\n".join(L), InlineKeyboardMarkup(rows)

    @staticmethod
    def _back_label(src):
        return {"A": "◀ 最近话题", "E": "◀ 最近变动"}.get(src, "◀ 备份清单")

    @staticmethod
    def _back_cb(gid, src, page):
        if src == "L":
            return "L:%s:%d" % (gid, page)
        return "%s:1" % src

    def _topic_images(self, gid, tid):
        raw = self.archive.load(gid, tid)
        topic = raw.get("topic") or self._live.get((str(gid), str(tid))) or {}
        return _topic_image_groups(topic)

    def _full_texts(self, gid, tid):
        """全文拆成几条消息: 头部 + 每个正文区块, 超长的那段自己再截。"""
        raw = self.archive.load(gid, tid)
        topic = raw.get("topic") or self._live.get((str(gid), str(tid))) or {}
        if not topic:
            return ["这条的存档文件不在了，先用卡片上的「跟服务器核对一次」看看服务器那边。"]
        gname = self.s.gnames.get(str(gid), str(gid))
        title = _plain(topic.get("title") or "").strip() or "(无标题)"
        head = ["💬 <b>%s</b>" % _html.escape(title[:90]),
                "星球：%s · %s · %s" % (_html.escape(gname),
                                        _html.escape(_author_of(topic)),
                                        _short(topic.get("create_time"))),
                _counts_line(topic)]
        if topic.get("digested"):
            head.append("★ 精华" + ("　📌 置顶" if topic.get("sticky") else ""))
        pieces = ["\n".join(head)]
        for label, text, images in topic_blocks(topic):
            body = _inline_html(text).strip()
            if _u16(body) > MAXLEN:      # 按 UTF-16 算, 不然一堆 emoji 会漏过判断被悄悄切掉
                body = _clip(body, MAXLEN - 200) + "\n（后面太长，省略了；完整正文在备份的 .md 里）"
            part = []
            if label:
                part.append("<b>【%s】</b>" % label)
            if body:
                part.append(_quote(body))
            elif images:
                part.append("（这一块只有图）")
            pieces.append("\n".join(part))
        files = collect_files(topic)
        if files:
            pieces.append("\n".join("📎 %s（%s）" % (_html.escape(nm), _size(sz))
                                    for _, nm, sz, _ in files))
        pieces.append("原文地址\n" + TOPIC_URL % tid)
        return _pack(pieces)

    def _comments_view(self, gid, tid, page, fetched=None, note=None):
        """评论页: 本地存档优先; 传了 fetched 就直接展示刚拉到的。"""
        raw = self.archive.load(gid, tid)
        rec = self.s.state.get("%s/%s" % (gid, tid)) or {}
        comments = fetched if fetched is not None else Archive.comments_of(raw)
        topic = raw.get("topic") or self._live.get((str(gid), str(tid))) or {}
        title = (_plain(topic.get("title") or "").strip()
                 or self.archive.title(gid, tid, rec))
        total = len(comments)
        have = Archive.comment_total(comments)     # 跟接口的 comments_count 同口径
        blocks, cur, size = [], [], 0
        for i, c in enumerate(comments, 1):        # 先渲染好每条, 再决定一页放几条
            b = [_fmt_comment(c, i)]
            for r in (c.get("replied_comments") or []):
                b.append(_fmt_comment(r, i, parent=c))
            b = "\n".join(b) + "\n"
            w = _u16(b)
            if cur and size + w > MAXLEN - 500:    # 留出页眉和计数行的位置
                blocks.append("\n".join(cur))
                cur, size = [], 0
            cur.append(b)
            size += w
        blocks.append("\n".join(cur))
        pages = max(1, len(blocks))
        page = int(page or 0)
        page = pages if page <= 0 else max(1, min(page, pages))
        L = ["💬 <b>%s</b> 的评论" % _html.escape(title[:60]), ""]
        if note:
            L += [note, ""]
        if not total:
            L.append("本地还没有这条话题的评论。")
            L.append("点下面的「从星球拉一次」，我当场抓一遍并存下来。")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 从星球拉一次", callback_data="Z:%s:%s" % (gid, tid))],
                [InlineKeyboardButton("◀ 回话题", callback_data="T:%s:%s:L:1" % (gid, tid))]])
            return "\n".join(L), kb
        L.append("%d 条评论%s · 第 %d/%d 页 · 按时间从早到晚" % (
            have, ("（含 %d 条楼中楼）" % (have - total)) if have > total else "",
            page, pages))
        if rec.get("cc") and int(rec["cc"]) > have:
            L.append("服务器上次显示 %d 条，本地少了 %d 条 —— 点下面的「拉最新」补齐。"
                     % (int(rec["cc"]), int(rec["cc"]) - have))
        elif rec.get("cc") and int(rec["cc"]) < have:
            L.append("服务器上次只显示 %d 条，本地多出 %d 条 —— 那边多半删过，"
                     "删掉的内容本地还留着，不用补。"
                     % (int(rec["cc"]), have - int(rec["cc"])))
        L.append("")
        L.append(blocks[page - 1])
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("◀ 上一页", callback_data="C:%s:%s:%d" % (gid, tid, page - 1)))
        nav.append(InlineKeyboardButton("%d/%d" % (page, pages), callback_data="noop"))
        if page < pages:
            nav.append(InlineKeyboardButton("下一页 ▶", callback_data="C:%s:%s:%d" % (gid, tid, page + 1)))
        kb = InlineKeyboardMarkup([
            nav,
            [InlineKeyboardButton("🔄 从星球拉最新评论", callback_data="Z:%s:%s" % (gid, tid))],
            [InlineKeyboardButton("◀ 回话题", callback_data="T:%s:%s:L:1" % (gid, tid))]])
        return "\n".join(L), kb

    async def _fetch_comments_and_show(self, gid, tid):
        raw = self.archive.load(gid, tid)
        stored = Archive.comments_of(raw)
        # 有本地存档就从最后一条往后取, 只要新评论; 没有就整段拉
        begin = None
        if stored:
            begin = max((c.get("create_time") or "") for c in stored) or None
        cli = self.s.cli
        fetched = await asyncio.to_thread(cli.comments, tid, 0, begin)
        if not fetched:
            note = "服务器这次没给新评论（可能本来就没有新的，或者请求没成功，原因在服务日志里）。"
            return self._comments_view(gid, tid, 0, fetched=stored, note=note)
        if raw:
            merged_raw, added = await asyncio.to_thread(self.archive.append_comments, gid, tid, fetched)
            comments = Archive.comments_of(merged_raw)
            have = Archive.comment_total(comments)
            note = ("✅ 刚补上 %d 条评论，本地现在 %d 条。" % (len(added), have)
                    if added else "服务器这次没给新评论，看到的还是本地这些。")
            rec = (self.s.state or {}).get("%s/%s" % (gid, tid))
            if rec is not None:          # 只动这个 dict, 不增删键 —— 哨兵那边正在遍历它
                rec["cc"] = max(int(rec.get("cc") or 0), have)
            return self._comments_view(gid, tid, 0, fetched=comments, note=note)
        note = "这条没备份到本地，下面是刚从星球拉的（不会存下来，想要存档就在星球页开自动推送）。"
        return self._comments_view(gid, tid, 0, fetched=fetched, note=note)

    def _rev_view(self, gid, tid, n):
        revs = self.archive.rev_numbers(gid, tid)
        rec = self.s.state.get("%s/%s" % (gid, tid)) or {}
        topic = self.archive.load(gid, tid).get("topic") or {}
        title = (_plain(topic.get("title") or "").strip()
                 or self.archive.title(gid, tid, rec))
        if not revs:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ 回话题", callback_data="T:%s:%s:L:1" % (gid, tid))]])
            return ("✏️ 「%s」还没有改动记录。\n\n"
                    "服务器上一发生修改，哨兵就会存一份修订快照，这里就能看到逐行差异。"
                    % _html.escape(title[:60])), kb
        n = int(n or 0)
        if n not in revs:
            n = revs[-1]
        idx = revs.index(n)
        prev = self.archive.load_rev(gid, tid, revs[idx - 1]) if idx else self.archive.load(gid, tid)
        cur = self.archive.load_rev(gid, tid, n)
        if not cur:
            # 快照刚被「保留条数」清掉, 缓存里还挂着: 拿空壳去比会显示成「整篇都是新增」
            self.archive.forget_revs(gid)
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("◀ 回话题", callback_data="T:%s:%s:L:1" % (gid, tid))]])
            return ("✏️ 「%s」这份改动快照已经不在了。\n\n"
                    "多半是被「保留条数」清掉了 —— 列表已经刷新，回话题页看还有哪几次。"
                    % _html.escape(title[:60])), kb
        diff = Archive.diff_topics(prev.get("topic") or {}, cur.get("topic") or {})
        when = (cur.get("_fetched_at") or "")[:16]
        L = ["✏️ <b>%s</b>" % _html.escape(title[:60]),
             "第 %d/%d 次改动%s" % (idx + 1, len(revs), (" · 快照存于 %s" % when) if when else ""), ""]
        L.append(_diff_text(diff))
        L.append("")
        if idx:
            L.append("（比的是上一次改动之前的样子）")
        else:
            L.append("（比的是最初备份下来的样子）")
        rows, row = [], []
        for k, rn in enumerate(revs, 1):
            row.append(InlineKeyboardButton(("第%d次" % k) if rn != n else ("·第%d次·" % k),
                                            callback_data="V:%s:%s:%d" % (gid, tid, rn)))
            if len(row) == 4:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("📄 看现在的全文", callback_data="F:%s:%s" % (gid, tid)),
                     InlineKeyboardButton("◀ 回话题", callback_data="T:%s:%s:L:1" % (gid, tid))])
        return "\n".join(L), InlineKeyboardMarkup(rows)

    def set_text(self, gid):
        g = str(gid)
        L = ["⚙ <b>「%s」抓取设置</b>" % _html.escape(self.s.gnames.get(g, g)), ""]
        changed, self._opt_change = self._opt_change, None
        if changed:
            key, value = changed
            L += ["✅ 刚把「%s」改成 %s。" % (self.OPT_NAMES[key], self._opt_value(key, value)),
                  self._opt_meaning(key, value, g), ""]
        L.append("点一行就换下一个值，改完立刻生效，也写回 watch.json，重启不丢。")
        L.append("想要列表里没有的值，点最下面那行，直接发个数字给我。")
        L.append("")
        for key in ("poll_interval", "page_size", "check_interval", "recent_days",
                    "max_comments", "sweep_every", "sweep_size",
                    "quiet_hours", "keep_media"):
            L.append("<b>%s（%s）</b> — %s" % (self.OPT_NAMES[key],
                                              self._opt_value(key, self.s.opt(g, key)),
                                              self.OPT_HELP[key]))
        L += ["", "现在：%s" % self.s.describe(g, with_name=False)]
        return "\n".join(L)

    def status_text(self):
        s = self.s
        state = list(s.state.values())          # 快照: 哨兵线程同时在改这个 dict
        now = time.time()
        auth = "有效" if now - (s._last_auth_ok or 0) < 3600 else "该复核了"
        quiet = self._fmt_quiet(s.cfg.get("quiet_hours"))
        events = len(getattr(s, "events", []) or [])
        gone = sum(1 for v in state if v.get("deleted"))
        L = ["📊 <b>哨兵状态</b>", "",
             "盯守星球：%d 个" % len(s.cfg.get("groups") or []),
             "备份话题：%d 条%s" % (len(state), ("（其中 %d 条已被服务器删掉）" % gone) if gone else ""),
             "凭证：%s" % auth,
             "夜间静默：%s" % quiet,
             "本次运行记录到 %d 次变动" % events]
        if not s.gnames:
            L.append("星球名单：还没拉到（凭证多半失效了，发 /login）")
        auto = [_html.escape(s.gnames.get(g, g)) for g, on in self.autopush.items() if on]
        L.append("自动推送：%s" % (", ".join(auto) if auto else "没开"))
        keeps = {g: int(v) for g, v in (s.cfg.get("group_keep") or {}).items() if v}
        L.append("限量星球：%s" % (
            ", ".join("%s→%d 条" % (_html.escape(s.gnames.get(g, g)), n) for g, n in keeps.items())
            if keeps else "都不限量"))
        L += ["", "<b>各星球的节奏</b>"]
        for g in (s.cfg.get("groups") or []):
            gname = s.gnames.get(str(g), str(g))
            last = (getattr(s, "last_poll", {}) or {}).get(str(g))
            L.append("· <b>%s</b> %s · %s" % (
                _html.escape(gname), s.describe(g, with_name=False),
                ("上次检查 " + _ago(last)) if last else "还没检查过"))
        return "\n".join(L)

    # ---- 动作 ----

    async def groups_text(self):
        """星球名单正常由抓取循环顺手填好; 万一还是空的就当场拉一次 ——
        新装的时候第一件事就是 /groups, 这时候循环可能还没跑起来。"""
        if not self.s.gnames:
            try:
                await asyncio.to_thread(self.s.ensure_groups, True)
            except Exception as e:
                log("[!] 拉星球列表失败: %r" % e)
        if not self.s.gnames:
            return GROUPS_EMPTY
        return ("你加入的星球（%d 个）\n\n"
                "点一个进去：翻这条星球的全部备份、开自动推送、调抓取节奏。\n"
                "想直接看别的星球最近在聊什么，回主菜单点「最近话题」。" % len(self.s.gnames))

    def cycle_opt(self, gid, key):
        """轮换某个星球的抓取设置: 候选值里取下一个, 到末尾回到开头。"""
        choices = self.OPT_CHOICES[key]
        cur = self.s.opt(gid, key)
        try:
            nxt = choices[(choices.index(cur) + 1) % len(choices)]
        except ValueError:
            nxt = choices[0]                 # 手动改成候选外的值, 从头开始
        self.s.set_opt(gid, key, nxt)
        self._opt_change = (key, nxt)
        self.save_pref()

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
        if self.autopush[key]:
            return ("「%s」自动推送已开启 🔔\n\n"
                    "这个星球的每个新话题，备份完成就发到这里，正文和图片一起。\n"
                    "它也已经加进盯守列表，开始按自己的节奏抓了。" % _html.escape(gname))
        return "「%s」自动推送已关闭 🔕\n\n新话题只存本地，不再发消息。" % _html.escape(gname)

    def cycle_alert(self, gid):
        """评论提醒四档循环: 都提醒 → 只看新增 → 只看减少 → 全关 → 都提醒。"""
        cur = self.s.opt(gid, "comment_alert")
        seq = self.ALERT_CHOICES
        nxt = seq[(seq.index(cur) + 1) % len(seq)] if cur in seq else seq[0]
        self.s.set_opt(gid, "comment_alert", nxt)
        self.save_pref()
        return nxt

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
            return ("「%s」改成只留最近 %d 条，已清掉 %d 条旧话题（对应的备份文件一起删了）。\n\n"
                    "删掉的内容找不回来；想留住就别设上限。" % (
                        _html.escape(gname), nxt, self.s.prune_group(gid, nxt)))
        return "「%s」改成不限量。\n\n刚才删掉的那些不会找回来，从现在起不再自动清理。" % _html.escape(gname)

    async def show_latest(self, chat_id, gid, n=10):
        """从服务器现场拉一页新话题: 先给一份可点的清单, 再问要不要全文。"""
        gname = self.s.gnames.get(gid, gid)
        await self._send(chat_id, "正在从「%s」拉最新 %d 条…" % (_html.escape(gname), n))
        topics = (await asyncio.to_thread(self.s.latest_page, gid, n) or [])[:n]
        if not topics:
            await self._send(chat_id, "没拉到话题。可能是这个星球本来就没内容，"
                                      "也可能是请求失败（具体原因在服务日志里）。")
            return
        self._remember_live(gid, topics)
        entries = [{"gid": str(gid), "tid": str(t.get("topic_id")),
                    "time": t.get("create_time") or "",
                    "cc": int(t.get("comments_count") or 0), "deleted": False,
                    "revs": 0, "rec": {}, "title": _plain(t.get("title") or "").strip()}
                   for t in topics]
        text, kb = self._list_view(entries, 1, "📥 「%s」服务器上的最新 %d 条"
                                   % (_html.escape(gname), len(entries)),
                                   origin="L", gid=gid,
                                   note="这是刚从服务器拉的，点一条看正文和图。")
        rows = list(kb.inline_keyboard)
        rows.insert(-1, [InlineKeyboardButton("📤 一条条发全文", callback_data="Q:%s" % gid)])
        await self._send(chat_id, text, InlineKeyboardMarkup(rows))

    async def dump_live(self, chat_id, gid):
        """把刚拉到的那一页, 按老样子一条条发全文 (正文 + 图)。
        一次最多发 12 条 —— 连发几十条正文加相册, 一定会撞上 Telegram 的限流,
        被限流的那部分就这样悄悄没了。剩下的说一声, 让用户自己决定还要不要。"""
        topics = [t for (g, _), t in self._live.items() if g == str(gid)]
        if not topics:
            await self.show_latest(chat_id, gid)
            return
        gname = self.s.gnames.get(gid, gid)
        cap = 12
        for t in topics[:cap]:
            await self._send_topic(chat_id, t, gname, gid)
            await asyncio.sleep(0.8)             # 别把频道刷到限流
        if len(topics) > cap:
            await self._send(chat_id, "一次最多发 %d 条（这次一共 %d 条），剩下的没发 —— "
                                      "连发这么多正文加相册会被 Telegram 限流。"
                                      "要点名看哪条，去「最近话题」里点它。" % (cap, len(topics)))

    def _remember_live(self, gid, topics):
        for t in topics:
            tid = str(t.get("topic_id"))
            if tid and tid != "None":
                self._live[(str(gid), tid)] = t
        while len(self._live) > 80:
            self._live.pop(next(iter(self._live)))

    def _remember_find(self, word, hits):
        self._find_seq += 1
        token = "f%d" % self._find_seq
        self._finds[token] = (word, hits)
        while len(self._finds) > 5:
            self._finds.pop(next(iter(self._finds)))
        return token

    # ---- 扫码登录 ----

    async def do_login(self, chat_id):
        """整条扫码链路跑在机器人线程里, 阻塞步骤全部 to_thread。"""
        if self._login_running:
            await self._send(chat_id, "已经有一个扫码流程在进行中, 请先完成或等它超时。")
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
        save_json(self.s.cred_path, {"access_token": token, "aduid": aduid,
                                     "login_way": "wechat_qr_bot",
                                     "login_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                     "_user": me}, indent=1)


def _size(n):
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return "?"
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1024.0 / 1024.0)


def _ts_text(ts):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(float(ts or 0)))
    except (TypeError, ValueError):
        return ""
