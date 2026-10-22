#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_tg_bot.py — 机器人离线自检 (不联网, 不需要凭证)

跑法:
  python3 -X utf8 test_tg_bot.py

覆盖:
  1 话题格式化: 问答全文 / 可折叠引用 / 链接与加粗转 HTML
  2 长文本截断: 不切半个标签, 引用自动闭合
  3 鉴权守卫: 白名单内放行, 白名单外拦截并回显对方 ID
  4 按钮键盘: 星球列表 / 单星球菜单 / 设置页 / 主菜单入口
  5 自动推送: 开启时把星球补进盯守列表并落盘
  6 保留条数: 循环切换并真实清理旧文件 (含修订快照)
  7 抓取设置: 逐星球覆盖 / 夜间静默 / 落盘 / 状态里读得回来
  8 投递链路: 哨兵线程 → 机器人事件循环
  9 检测并入抓取轮: 零请求比对 / 窗口内缺席才确认 / 掉出窗口不碰
  10 限流与凭证失效: 都不许把话题误判成被删除
  11 凭证自检缓存: 缓存期内不重复查 / 请求失败或过期才真查
  12 /groups: 名单没填上时当场拉 / 拉不到给出凭证提示
  13 评论变动: 涨了列出谁说了什么 / 掉了列出少了谁 / 上限挡住时如实说
  14 改动: 存修订快照 + 逐行差异
  15 备份清单: 分页 / 标题 / 标记 / 回调
  16 话题卡片: 元信息 / 入口按钮 / 删除了也看得到
  17 评论页: 分页 / 楼中楼 / 从服务器补评论
  18 改动对比页: 只列变化的区块
  19 全文页: 区块分开 / 图与附件 / 原文链接
  20 搜索: 按标题找得到, 找不到说得清
  21 归档时把标题记进 state
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from collections import deque
from datetime import datetime, timezone

# Windows + 代理软件常把 "[::1]" 写进 NO_PROXY, httpx 解析不了会直接崩
if "[::1]" in (os.environ.get("NO_PROXY") or ""):
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import telegram
from telegram.ext import ApplicationHandlerStop

import tg_bot
import tg_topics
import zsxq_archive as za
import zsxq_watch as w

TMP = tempfile.mkdtemp(prefix="zsxq-bot-check-")
ok = 0


def check(name, cond, extra=""):
    global ok
    if not cond:
        print("  ✗ %s %s" % (name, extra))
        sys.exit(1)
    ok += 1
    print("  ✓ %s" % name)


class FakeSentinel:
    pass


def fake_sentinel(**cfg):
    """造一个只带真实逻辑、不联网的哨兵: 请求由各用例自己的假 Client 顶替。"""
    s = FakeSentinel()
    s.out = tempfile.mkdtemp(dir=TMP, prefix="s-")
    s.state = {}
    s.gnames = {"10001": "星球甲"}
    # 深拷一份: 浅拷的话 group_opts 会是同一个 dict, 一个用例改了会串到下一个
    s.cfg = json.loads(json.dumps(w.DEFAULT_CFG))
    s.cfg.update(cfg)
    s.notes = []
    s.notify = lambda title, text="", skip_telegram=False: s.notes.append(title + "\n" + text)
    s.save_state = lambda: None
    s.events = deque(maxlen=200)
    s.last_poll = {}
    s.archive = tg_topics.Archive(s.out)
    s._auth_fail_ts = s._last_auth_ok = 0.0
    s._auth_dirty = False
    s._last_prune = 0.0
    s.bot = None
    s.archive_topic = lambda gid, gname, topic: s.state.__setitem__(
        "%s/%s" % (gid, topic.get("topic_id")),
        {"time": topic.get("create_time"), "mtime": topic.get("modify_time") or "",
         "cc": topic.get("comments_count") or 0,
         "title": za.topic_title({"title": topic.get("title") or ""})})
    for n in ("opt", "set_opt", "in_quiet", "latest_page", "poll_group", "check_window",
              "on_detail", "on_comment_change", "comment_delta", "on_edited", "on_deleted",
              "sweep_comments", "sweep_one",
              "auth_ok", "describe", "autopush_on", "add_event"):
        setattr(s, n, getattr(w.Sentinel, n).__get__(s))
    s.topic_link = w.Sentinel.topic_link       # staticmethod, 不要绑到实例上
    return s


def new_bot(sent, **attrs):
    """不启线程的机器人: 只测页面渲染, 不碰 telegram 网络。"""
    b = tg_bot.TgBot.__new__(tg_bot.TgBot)
    b.s = sent
    b.archive = sent.archive if getattr(sent, "archive", None) else tg_topics.Archive(sent.out)
    b.autopush = {}
    b._live = {}
    b._finds = {}
    b._find_seq = 0
    b._opt_change = None
    for k, v in attrs.items():
        setattr(b, k, v)
    return b


def comment(cid, who, text, t="2026-10-15T09:00:00", replies=None):
    c = {"comment_id": cid, "owner": {"name": who}, "text": text, "create_time": t}
    if replies:
        c["replied_comments"] = replies
    return c


def write_raw(sent, gid, tid, topic, comments=None):
    path = os.path.join(sent.out, "raw", str(gid), "%s.json" % tid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    za.save_json(path, {"topic": topic, "comments": comments or [],
                        "_group": sent.gnames.get(str(gid), str(gid)),
                        "_fetched_at": "2026-10-15 09:30:00"})
    return path


def callbacks(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def html_ok(s):
    """简易校验: 标签必须成对闭合, 正文里不能有裸的 < 。
    Telegram 收到半个标签会直接拒收整条消息, 界面上就是「点了没反应」。"""
    import re as _re
    stack, pos = [], 0
    for m in _re.finditer(r"<[^>]*>", s):
        if "<" in s[pos:m.start()]:
            return False
        tag = m.group(0)
        name = _re.match(r"</?([A-Za-z][\w-]*)", tag).group(1).lower()
        if tag.startswith("</"):
            if not stack or stack[-1] != name:
                return False
            stack.pop()
        elif not tag.endswith("/>"):
            stack.append(name)
        pos = m.end()
    return "<" not in s[pos:] and not stack


# ---------- 1 话题格式化 ----------
s = FakeSentinel()
s.gnames = {"10001": "星球甲", "10002": "星球乙"}
s.cfg = {"groups": ["10001"], "group_keep": {}, "_config_path": os.path.join(TMP, "watch.json"),
         "notify": {"telegram": "123456:FAKE-TOKEN"}, "bot_users": ["2013037643"]}
s.state = {"10001/%d" % i: {"time": "2026-07-%02dT10:00:00" % i,
                            "file": os.path.join("g", "t%d.md" % i)} for i in range(1, 60)}
s.out, s.state_path = TMP, os.path.join(TMP, "state.json")
s.cred_path, s._last_auth_ok = os.path.join(TMP, "cred.json"), time.time()
for n in ("save_state", "rebuild_index", "prune_group", "opt", "set_opt", "describe"):
    setattr(s, n, getattr(w.Sentinel, n).__get__(s))
s.archive = tg_topics.Archive(TMP)
s.latest_page = lambda gid, count=10: []
s.hot_reload_cred = lambda: True

bot = tg_bot.TgBot(s)

fmt = tg_bot._fmt_topic_brief(
    {"topic_id": "555", "create_time": "2026-10-14T11:07:00", "title": "截断的预览...",
     "question": {"text": "提问正文很长 <e type=\"text_bold\" title=\"%E9%87%8D%E7%A3%85\"/> 结尾",
                  "owner": {"name": "读者"}},
     "answer": {"text": "回答里有 <e type=\"web\" href=\"https%3A%2F%2Fexample.com\" title=\"%E7%A4%BA%E4%BE%8B\"/>",
                "owner": {"name": "马督工"}}}, "星球甲")
check("问答全文进正文", "提问正文很长" in fmt and "截断的预览" in fmt.splitlines()[0])
check("可折叠引用", tg_bot.BQ_OPEN in fmt
      and fmt.count(tg_bot.BQ_OPEN) == fmt.count(tg_bot.BQ_CLOSE))
check("加粗转 HTML", "<b>重磅</b>" in fmt)
check("链接转 HTML", '<a href="https://example.com">示例</a>' in fmt)

# ---------- 2 长文本截断 ----------
long_one = tg_bot._clip(tg_bot._fmt_topic_brief(
    {"topic_id": "9", "create_time": "2026-01-01T00:00:00",
     "talk": {"text": "x" * 6000, "owner": {"name": "a"}}}, "g"))
check("超长截断补闭合标签", len(long_one) <= tg_bot.MAXLEN + len(tg_bot.BQ_CLOSE)
      and long_one.count(tg_bot.BQ_OPEN) == long_one.count(tg_bot.BQ_CLOSE))

# 切点正落在 <a href="..."> 中间: 宁可少几个字, 也不能吐半个标签
head = "字" * (tg_bot.MAXLEN - 10)
tricky = head + '<a href="https://example.com/very/long/path">链接</a>'
cut = tg_bot._clip(tricky)
check("切在标签里就往前挪", "<a href" not in cut.split("字" * 10)[-1]
      and cut.count("<a ") == cut.count("</a>"))
check("截断后长度没超上限", len(cut) <= tg_bot.MAXLEN + 8)
# Telegram 按 UTF-16 单元计数, emoji 占两个 —— 纯 emoji 的长文也要截得下来
emoji_cut = tg_bot._clip("😀" * 3000)
check("emoji 按 UTF-16 口径截断", tg_bot._u16(emoji_cut) <= tg_bot.MAXLEN)

# ---------- 3 鉴权守卫 ----------
def make_update(uid):
    u = telegram.User(id=uid, first_name="X", is_bot=False)
    c = telegram.Chat(id=uid, type="private")
    m = telegram.Message(message_id=1, date=datetime.now(timezone.utc), chat=c,
                         from_user=u, text="/menu")
    return telegram.Update(update_id=uid, message=m)


refused = []


async def _noop(_):
    return None


bot._send = lambda chat_id, text, kb=None: _noop(refused.append((chat_id, text)))

asyncio.run(bot.guard(make_update(2013037643), None))
check("白名单内放行", not refused)
try:
    asyncio.run(bot.guard(make_update(999999), None))
    check("白名单外拦截", False)
except ApplicationHandlerStop:
    check("白名单外拦截并回显 ID", refused and "你的 ID: 999999" in refused[-1][1])

# ---------- 4 键盘 ----------
keys = callbacks(bot.kb_groups())
check("星球列表按钮", {"g:10001", "g:10002"} <= set(keys))
keys = callbacks(bot.kb_group("10001"))
check("星球页入口齐了", {"L:10001:1", "P:10001", "n:10001", "a:10001",
                      "k:10001", "s:10001"} <= set(keys))
keys = callbacks(bot.kb_menu())
check("主菜单有最近话题与变动", {"A:1", "E:1"} <= set(keys))
keys = callbacks(bot.kb_set("10001"))
check("设置页七项都能点", {"i:10001", "p:10001", "c:10001", "d:10001",
                        "m:10001", "q:10001", "w:10001"} <= set(keys))

# ---------- 5 自动推送联动 ----------
bot.toggle_autopush("10002")
saved = json.load(open(s.cfg["_config_path"], encoding="utf-8"))
check("开启时补进盯守列表并落盘",
      "10002" in s.cfg["groups"] and "10002" in saved["groups"]
      and saved["bot_autopush"]["10002"])

# ---------- 6 保留条数清理 ----------
gd = os.path.join(TMP, "g")
os.makedirs(gd, exist_ok=True)
for i in range(1, 60):
    open(os.path.join(gd, "t%d.md" % i), "w").write("x")
revp = os.path.join(TMP, "raw", "10001", "1__rev_1.json")
os.makedirs(os.path.dirname(revp), exist_ok=True)
za.save_json(revp, {"topic": {}})
bot.cycle_keep("10001")
check("只留 50 条并清理旧文件",
      s.cfg["group_keep"]["10001"] == 50 and len(s.state) == 50 and len(os.listdir(gd)) == 50)
check("修订快照跟着一起清", not os.path.exists(revp))
saved = json.load(open(s.cfg["_config_path"], encoding="utf-8"))
check("保留条数落盘", saved.get("group_keep", {}).get("10001") == 50)

# ---------- 7 抓取设置 ----------
bot.cycle_opt("10001", "poll_interval")
check("间隔可逐星球覆盖", s.opt("10001", "poll_interval") == 45)
bot.cycle_opt("10001", "page_size")
bot.cycle_opt("10001", "max_comments")
saved = json.load(open(s.cfg["_config_path"], encoding="utf-8"))
check("抓取设置逐项落盘", saved["group_opts"]["10001"] ==
      {"poll_interval": 45, "page_size": 30, "max_comments": 20})
check("状态里读得回节奏", "45" in s.describe("10001") and "30 条" in s.describe("10001"))
bot.cycle_opt("10001", "quiet_hours")
check("夜间静默可切换", s.opt("10001", "quiet_hours") == [23, 7]
      and "23 点–7 点" in bot.set_text("10001"))
bot.cycle_keep("10001")          # 顺手验证「刚改了」只回显一次
check("刚改了那句只出现一次", "刚把「抓取间隔」" not in bot.set_text("10001"))

# ---------- 8 投递链路 (哨兵同步线程 → 机器人事件循环) ----------
sent_cfg = dict(w.DEFAULT_CFG)
sent_cfg.update({"_config_path": os.path.join(TMP, "w2.json"), "groups": ["10001"],
                 "notify": {"telegram": "123456:FAKE-TOKEN"}, "bot_users": ["2013037643"]})
json.dump({"access_token": "X", "aduid": "Y"}, open(os.path.join(TMP, "cred2.json"), "w"))
sent = w.Sentinel(os.path.join(TMP, "cred2.json"), TMP, sent_cfg)
sent.gnames = {"10001": "星球甲"}
sent.auth_ok = lambda: True
sent.latest_page = lambda gid, count=None: [
    {"topic_id": "777", "create_time": "2026-10-15T10:00:00",
     "talk": {"text": "正文", "owner": {"name": "甲"}}}]
sent.archive_topic = lambda gid, gname, topic: sent.state.__setitem__(
    "%s/%s" % (gid, topic["topic_id"]), {"time": topic["create_time"]})
sent.rebuild_index = lambda: None

got = []
sent.bot = bot
pushed = []
bot.push_topic = lambda topic, gname, gid=None: pushed.append((topic, gname, gid))
bot.autopush["10001"] = True
sent.poll_group("10001")
check("新话题投递到机器人", len(pushed) == 1
      and "正文" in tg_bot._fmt_topic_brief(pushed[0][0], pushed[0][1])
      and pushed[0][2] == "10001")
check("新话题记进变动列表", sent.events and sent.events[-1]["kind"] == "new")
check("看过列表就记下时间", sent.last_poll.get("10001"))

# 跨线程投递本身 (loop 在别的线程里跑)
loop = asyncio.new_event_loop()
import threading
threading.Thread(target=loop.run_forever, daemon=True).start()
bot._loop, bot.push_chat_id = loop, lambda: 555


async def _record(chat_id, text, kb=None):
    got.append("threaded:" + text)


bot._send = _record
got.clear()
bot.push_text = tg_bot.TgBot.push_text.__get__(bot)
bot.push_text("跨线程")
for _ in range(50):
    if got:
        break
    time.sleep(0.05)
check("跨线程投递 (run_coroutine_threadsafe)", got == ["threaded:跨线程"], repr(got))

# 评论变动推送: 哨兵线程调 push_comment_change, 聊天里要出现「谁说了什么」和跳转按钮
msgs = []


async def _record_msg(chat_id, text, kb=None):
    msgs.append((text, kb))


bot._send = _record_msg
old_push_chat, bot.push_chat_id = bot.push_chat_id, lambda: 555
bot.push_comment_change("10001", "t9", "标题九", "星球甲", 5,
                        [(comment(9, "赵六", "刚说了一句"), None)], [], True)
for _ in range(50):
    if msgs:
        break
    time.sleep(0.05)
push_text = msgs[0][0] if msgs else ""
check("评论变动推到聊天里", "多了 1 条评论" in push_text and "赵六" in push_text
      and "刚说了一句" in push_text)
check("评论推送带跳转按钮",
      "C:10001:t9:0" in [b.callback_data for row in msgs[0][1].inline_keyboard for b in row])
bot.push_chat_id = old_push_chat
loop.call_soon_threadsafe(loop.stop)

# ---------- 9 检测并入抓取轮 ----------
class FakeBot:
    def __init__(self):
        self.autopush = {}
        self.pushed = []

    def admins_or_any_chat(self):
        return True

    def push_topic(self, topic, gname, gid=None):
        self.pushed.append(("topic", topic.get("topic_id"), gid))

    def push_deleted(self, gid, tid, title, gname):
        self.pushed.append(("gone", tid, gname))

    def push_comment_change(self, gid, tid, title, gname, total, added, removed, complete=True):
        self.pushed.append(("comment", tid, len(added), len(removed), complete))

    def push_edit_change(self, gid, tid, title, gname, rev_n, diff):
        self.pushed.append(("edit", tid, rev_n, diff))


s2 = fake_sentinel(recent_days=30, check_interval=300, check_batch=10)
s2.bot = FakeBot()
os.makedirs(os.path.join(s2.out, "raw", "10001"), exist_ok=True)
s2.state = {
    "10001/alive":    {"time": "2026-10-10T10:00:00", "mtime": "", "cc": 3},
    "10001/edited":   {"time": "2026-10-11T10:00:00", "mtime": "", "cc": 3},
    "10001/vanished": {"time": "2026-10-12T10:00:00", "mtime": "", "cc": 3},
    "10001/old":      {"time": "2026-09-01T10:00:00", "mtime": "", "cc": 3},
}
s2.latest_page = lambda gid, count=None: [
    {"topic_id": "new", "create_time": "2026-10-13T10:00:00", "comments_count": 0},
    {"topic_id": "edited", "create_time": "2026-10-11T10:00:00", "comments_count": 5,
     "modify_time": "2026-10-19T08:00:00",
     "talk": {"text": "改过的正文", "owner": {"name": "甲"}}},
    {"topic_id": "alive", "create_time": "2026-10-10T10:00:00", "comments_count": 3}]


class FakeCli:
    def __init__(self):
        self.hit = []

    def req(self, path, retries=3):
        self.hit.append(path)
        if path.endswith("/vanished"):
            return {"succeeded": False, "code": 1007, "error": "主题不存在或已被删除"}
        return {"succeeded": True, "resp_data": {"topic": {"comments_count": 5,
                                                           "modify_time": "2026-10-19T08:00:00",
                                                           "talk": {"text": "改过的正文"}}}}

    def comments(self, tid, limit=0, begin=None):
        self.hit.append("comments:%s" % tid)
        return []

    def whoami(self):
        return {"name": "me"}


s2.cli = FakeCli()
s2.poll_group("10001")
check("列表里已有的老话题不发请求", "/v2/topics/alive" not in s2.cli.hit)
check("掉出窗口的老话题根本不碰", "/v2/topics/old" not in s2.cli.hit)
check("窗口内缺席 + 明确报不存在 → 判删除", s2.state["10001/vanished"].get("deleted"))
check("删除时机器人收到了跳转推送", ("gone", "vanished", "星球甲") in s2.bot.pushed)
check("第一次被编辑也能发现", any("有变动" in t for t in s2.notes))
check("编辑存下修订快照", os.path.exists(os.path.join(s2.out, "raw", "10001", "edited__rev_1.json")))
check("编辑推送带着逐行差异", any(p[0] == "edit" and p[1] == "edited" and p[3]
                              for p in s2.bot.pushed))
check("新话题照常入库", "10001/new" in s2.state)
check("变动列表记下了编辑和删除",
      {"edit", "gone"} <= {e["kind"] for e in s2.events})

# ---------- 10 限流/5xx 没有"话题不存在"的明确信号 → 不当成删除 ----------
s5 = fake_sentinel(recent_days=30, check_interval=300)
s5.state = {"10001/busy": {"time": "2026-10-12T10:00:00", "mtime": "", "cc": 1}}
s5.latest_page = lambda gid, count=None: [
    {"topic_id": "other", "create_time": "2026-10-10T10:00:00", "comments_count": 1}]


class FakeCliBusy:
    def req(self, path, retries=3):
        return {"succeeded": False, "error": "HTTP 403"}

    def whoami(self):
        return {"name": "me"}


s5.cli = FakeCliBusy()
s5.poll_group("10001")
check("限流/5xx 不判定删除",
      not s5.state["10001/busy"].get("deleted") and not any("删除" in t for t in s5.notes))

# ---------- 11 凭证自检缓存 ----------
class CntCli:
    def __init__(self):
        self.n = 0

    def whoami(self):
        self.n += 1
        return {"name": "me"}


s3 = fake_sentinel()
s3.cfg = {"auth_cache_ttl": 1800}
s3.cli = CntCli()
check("首次凭证自检真查一次", s3.auth_ok() is True and s3.cli.n == 1)
check("缓存期内不重复查", s3.auth_ok() is True and s3.cli.n == 1)
s3._auth_dirty = True
check("请求失败后强制重验", s3.auth_ok() is True and s3.cli.n == 2)
s3._last_auth_ok = time.time() - 99999
check("缓存过期后重验", s3.auth_ok() is True and s3.cli.n == 3)
s3.cli.whoami = lambda: {}
check("凭证真失效时返回 False", s3.auth_ok(force=True) is False)

# ---------- 12 凭证失效时不要把话题误判成被删除 ----------
s4 = fake_sentinel(recent_days=30, check_interval=300, auth_cache_ttl=0)
s4.state = {"10001/ddd": {"time": "2026-10-12T10:00:00", "mtime": "", "cc": 1}}
s4.latest_page = lambda gid, count=None: [
    {"topic_id": "other", "create_time": "2026-10-10T10:00:00", "comments_count": 1}]


class FakeCliDead:
    def req(self, path, retries=3):
        return {"succeeded": False, "error": "HTTP 401"}

    def comments(self, tid, limit=0, begin=None):
        return []

    def whoami(self):
        return {}


s4.cli = FakeCliDead()
s4.poll_group("10001")
check("凭证失效时不判定删除", not s4.state["10001/ddd"].get("deleted"))
check("只发凭证失效告警, 不发删除告警",
      any("凭证" in t for t in s4.notes) and not any("删除" in t for t in s4.notes))

# ---------- 13 评论变动 ----------
s8 = fake_sentinel()
s8.bot = FakeBot()
old = [comment(1, "张三", "第一条", "2026-10-15T09:00:00"),
       comment(2, "李四", "第二条", "2026-10-15T09:10:00"),
       comment(3, "王五", "第三条", "2026-10-15T09:20:00")]
topic = {"topic_id": "t1", "create_time": "2026-10-15T08:00:00", "title": "标题甲",
         "comments_count": 3}
write_raw(s8, "10001", "t1", topic, old)
s8.state = {"10001/t1": {"time": topic["create_time"], "mtime": "", "cc": 3,
                         "title": "标题甲"}}


class GrowCli:
    """评论从 3 条涨到 5 条; begin_time 之后只返回最后一条和两条新的。"""
    def __init__(self):
        self.args = []

    def comments(self, tid, limit=0, begin=None):
        self.args.append((limit, begin))
        return [comment(3, "王五", "第三条", "2026-10-15T09:20:00"),
                comment(4, "赵六", "第四条新来的", "2026-10-15T10:00:00"),
                comment(5, "钱七", "第五条新来的", "2026-10-15T10:05:00")]


s8.cli = GrowCli()
s8.on_comment_change("10001", "t1", "10001/t1", dict(topic, comments_count=5), 3, 5)
check("涨了会去拉新评论", s8.cli.args and s8.cli.args[0][1] == "2026-10-15T09:20:00")
check("只算了真正新增的两条",
      s8.bot.pushed[-1][0] == "comment" and s8.bot.pushed[-1][2] == 2)
check("只拉到尾巴时不谎报老评论消失", s8.bot.pushed[-1][3] == 0)
check("新评论并进了本地存档",
      len(s8.archive.load("10001", "t1")["comments"]) == 5)
check("告警里带着是谁说了什么",
      any("赵六" in t and "第四条新来的" in t for t in s8.notes))
check("变动列表记了评论变动", s8.events[-1]["kind"] == "comment")

# 评论被删: 整段拉一遍, 指出少了谁
s10 = fake_sentinel()
s10.bot = FakeBot()
write_raw(s10, "10001", "t3", {"topic_id": "t3", "comments_count": 3}, old)
s10.state = {"10001/t3": {"time": "2026-10-15T08:00:00", "mtime": "", "cc": 3}}


class ShrinkCli:
    def comments(self, tid, limit=0, begin=None):
        return old[:2]           # 第三条没了


s10.cli = ShrinkCli()
s10.on_comment_change("10001", "t3", "10001/t3", {"topic_id": "t3", "title": "标题丙"}, 3, 2)
check("掉评论时指出少了谁",
      s10.bot.pushed[-1][0] == "comment" and s10.bot.pushed[-1][3] == 1
      and any("王五" in t and "不见了" in t for t in s10.notes))
check("合并评论只增不减", len(s10.archive.load("10001", "t3")["comments"]) == 3)

# 评论上限挡住: 如实说比不全, 不谎报谁不见了
s11 = fake_sentinel(max_comments=20)
s11.bot = FakeBot()
write_raw(s11, "10001", "t4", {"topic_id": "t4", "comments_count": 50}, old)
s11.state = {"10001/t4": {"time": "2026-10-15T08:00:00", "mtime": "", "cc": 50}}


class CappedCli:
    def comments(self, tid, limit=0, begin=None):
        return old[:1]


s11.cli = CappedCli()
s11.on_comment_change("10001", "t4", "10001/t4", {"topic_id": "t4", "title": "标题丁"}, 50, 49)
check("上限挡住时不谎报删除", s11.bot.pushed[-1][3] == 0 and s11.bot.pushed[-1][4] is False)


# ---------- 13b 评论提醒四档: 都提醒 / 只看新增 / 只看减少 / 全关 ----------
class NoFetchCli:
    """一次请求都不该发生时的替身: 被调用就说明这档白花了请求。"""
    def __init__(self):
        self.n = 0

    def comments(self, tid, limit=0, begin=None):
        self.n += 1
        return []


def alert_run(mode, old_cc, new_cc, cli):
    s = fake_sentinel(comment_alert=mode)
    s.bot = FakeBot()
    write_raw(s, "10001", "m1", {"topic_id": "m1", "title": "标题", "comments_count": old_cc}, old)
    s.state = {"10001/m1": {"time": "2026-10-15T08:00:00", "mtime": "", "cc": old_cc}}
    s.cli = cli
    s.on_comment_change("10001", "m1", "10001/m1", {"topic_id": "m1", "title": "标题"}, old_cc, new_cc)
    return s


g1 = alert_run("added", 3, 5, GrowCli())
check("只看新增: 涨了照样提醒", g1.bot.pushed and g1.bot.pushed[-1][2] == 2)
g2 = alert_run("added", 3, 2, NoFetchCli())
check("只看新增: 跌了不提醒, 一个请求都不花", g2.cli.n == 0 and not g2.bot.pushed)
check("只看新增: 跌了也记一笔变动", g2.events[-1]["kind"] == "comment")

r1 = alert_run("removed", 3, 2, ShrinkCli())
check("只看减少: 跌了会指出少了谁", bool(r1.bot.pushed) and r1.bot.pushed[-1][3] == 1)
r2 = alert_run("removed", 3, 5, GrowCli())
check("只看减少: 涨了不出声", not r2.bot.pushed)
check("只看减少: 涨了的新评论照样进存档",
      len(r2.archive.load("10001", "m1")["comments"]) == 5)

o1 = alert_run("off", 3, 5, NoFetchCli())
check("全关: 不拉评论也不提醒", o1.cli.n == 0 and not o1.bot.pushed)
check("全关: 变动列表仍然记着数字", o1.events[-1]["kind"] == "comment")

# 四档循环 + 按钮上写着当前档位
alert_bot = new_bot(fake_sentinel(), autopush_path=os.path.join(TMP, "w_alert.json"))
seen = []
for _ in range(5):
    seen.append(alert_bot.cycle_alert("10001"))
labels = [b.text for row in alert_bot.kb_group("10001").inline_keyboard for b in row]
check("评论提醒四档循环", seen == ["added", "removed", "off", "both", "added"],
      str(seen))
check("星球页那一行写着当前档位", any("评论提醒 · 只看新增" in x for x in labels), str(labels))

# 「只看新增」漏掉的评论变少, 卡片和评论页要能自己看出来
five = old + [comment(4, "赵六", "第四条", "2026-10-15T09:30:00"),
              comment(5, "钱七", "第五条", "2026-10-15T09:40:00")]
s26 = fake_sentinel(comment_alert="added")
write_raw(s26, "10001", "d1", {"topic_id": "d1", "title": "删过评论的题",
                               "comments_count": 3}, five)
s26.state = {"10001/d1": {"time": "2026-10-15T08:00:00", "cc": 3, "title": "删过评论的题"}}
b26 = new_bot(s26)
card_text, _ = b26._card_view("10001", "d1", "L", 1)
com_text, _ = b26._comments_view("10001", "d1", 1)
check("卡片会写服务器上比本地少", "服务器只有 3 条" in card_text)
check("评论页点明本地多出来的来路", "本地多出 2 条" in com_text)

# ---------- 13c 评论抽查: 评论数看不出来的那部分 ----------
class ReplyCli:
    """评论数没变, 但有人往楼中楼里回了一句。"""
    def __init__(self):
        self.n = 0

    def comments(self, tid, limit=0, begin=None):
        self.n += 1
        head = dict(old[0], replied_comments=[
            comment(77, "楼里的人", "补一句", "2026-10-15T11:00:00")])
        return [head, old[1], old[2]]


def sweep_sentinel(**cfg):
    s = fake_sentinel(**cfg)
    s.bot = FakeBot()
    write_raw(s, "10001", "s1", {"topic_id": "s1", "title": "有楼中楼的题",
                                 "comments_count": 3}, old)
    s.state = {"10001/s1": {"time": "2026-10-15T08:00:00", "mtime": "", "cc": 3,
                            "title": "有楼中楼的题"}}
    return s


sw = sweep_sentinel(recent_days=30, sweep_interval=1800, sweep_batch=1)
sw.cli = ReplyCli()
sw.sweep_comments("10001")
check("抽查发现新到的楼中楼",
      bool(sw.bot.pushed) and sw.bot.pushed[-1][2] == 1
      and any("补一句" in t for t in sw.notes))
check("楼中楼进了存档",
      any(r.get("comment_id") == 77
          for c in sw.archive.load("10001", "s1")["comments"]
          for r in (c.get("replied_comments") or [])))
check("抽查把评论数对齐到平台口径 (含楼中楼), 下一轮不会再报一遍",
      sw.state["10001/s1"]["cc"] == 4)
ran = sw.cli.n
sw.sweep_comments("10001")
check("刚抽查过的话题不会连着再翻", sw.cli.n == ran)

sa = sweep_sentinel(recent_days=30, sweep_interval=1800, comment_alert="added")
sa.cli = ShrinkCli()
sa.sweep_comments("10001")
check("只看新增: 抽查发现变少也不打扰", not sa.bot.pushed)

so = sweep_sentinel(recent_days=30, sweep_interval=1800, comment_alert="off")
so.cli = NoFetchCli()
so.sweep_comments("10001")
check("全关: 抽查一个请求都不花", so.cli.n == 0)

sc = fake_sentinel(recent_days=30, sweep_interval=1800, max_comments=20)
sc.bot = FakeBot()
write_raw(sc, "10001", "s2", {"topic_id": "s2", "title": "很多评论的题",
                              "comments_count": 50}, old)
sc.state = {"10001/s2": {"time": "2026-10-15T08:00:00", "mtime": "", "cc": 50}}


class ShortCli:
    def comments(self, tid, limit=0, begin=None):
        return old[:1]


sc.cli = ShortCli()
sc.sweep_comments("10001")
check("抽查被上限截断时什么都不说", not sc.bot.pushed)

# ---------- 13d 楼中楼也算进 comments_count ----------
# 拿 4745 条真实存档核过: 接口的 comments_count = 主评论 + 楼中楼, 差额只来自删除
withrep = [comment(1, "甲", "主评论", replies=[comment(2, "乙", "楼中楼一"),
                                              comment(3, "丙", "楼中楼二")]),
           comment(4, "丁", "第二条主评论")]
s30 = fake_sentinel()
write_raw(s30, "10001", "r1", {"topic_id": "r1", "title": "有楼中楼的题",
                               "comments_count": 4}, withrep)
s30.state = {"10001/r1": {"time": "2026-10-15T08:00:00", "cc": 4, "title": "有楼中楼的题"}}
b30 = new_bot(s30)
card30, _ = b30._card_view("10001", "r1", "L", 1)
com30, _ = b30._comments_view("10001", "r1", 1)
check("卡片按「主评论+楼中楼」算条数",
      "本地存了 4 条评论" in card30 and "含 2 条楼中楼" in card30)
check("条数对得上就不喊本地不全", "本地不全" not in card30)
check("评论页报总数也带楼中楼", "4 条评论（含 2 条楼中楼）" in com30)


class DropReplyCli:
    """少了一条楼中楼: 主评论一条没少, 但平台的总数从 4 掉到 3。"""
    def comments(self, tid, limit=0, begin=None):
        return [dict(withrep[0], replied_comments=[withrep[0]["replied_comments"][0]]),
                withrep[1]]


s31 = fake_sentinel(recent_days=30)
s31.bot = FakeBot()
write_raw(s31, "10001", "r2", {"topic_id": "r2", "title": "有楼中楼的题",
                               "comments_count": 4}, withrep)
s31.state = {"10001/r2": {"time": "2026-10-15T08:00:00", "mtime": "", "cc": 4}}
s31.cli = DropReplyCli()
s31.on_comment_change("10001", "r2", "10001/r2", {"topic_id": "r2", "title": "有楼中楼的题"}, 4, 3)
check("少的是楼中楼也说得出来",
      bool(s31.bot.pushed) and s31.bot.pushed[-1][3] == 1 and s31.bot.pushed[-1][4] is True)

# ---------- 14 改动对比 ----------
s12 = fake_sentinel()
s12.bot = FakeBot()
base = {"topic_id": "t5", "create_time": "2026-10-15T08:00:00", "title": "老标题",
        "talk": {"text": "第一行\n第二行", "owner": {"name": "甲"}}}
write_raw(s12, "10001", "t5", base)
s12.state = {"10001/t5": {"time": base["create_time"], "mtime": "", "cc": 0}}


class EditCli:
    def __init__(self):
        self.topic = dict(base, money=None, modify_time="2026-10-19T08:00:00",
                          title="新标题",
                          talk={"text": "第一行\n第二行改过", "owner": {"name": "甲"}})

    def req(self, path, retries=3):
        return {"succeeded": True, "resp_data": {"topic": self.topic}}

    def comments(self, tid, limit=0, begin=None):
        return []


s12.cli = EditCli()
s12.on_detail("10001", "t5", "10001/t5", dict(base, modify_time="2026-10-19T08:00:00"))
diff = s12.bot.pushed[-1][3]
check("改动算出逐行差异",
      any(d["label"] == "正文" and d["added"] == 1 and d["removed"] == 1 for d in diff)
      and any(d["label"] == "标题" for d in diff))
check("修订快照存了第 1 份",
      os.path.exists(os.path.join(s12.out, "raw", "10001", "t5__rev_1.json"))
      and s12.state["10001/t5"]["revs"] == 1)

# ---------- 15 备份清单 ----------
s13 = fake_sentinel()
write_raw(s13, "10001", "a1", {"topic_id": "a1", "title": "第一题",
                               "create_time": "2026-10-10T10:00:00",
                               "talk": {"text": "正文一", "owner": {"name": "甲"}}})
s13.state = {"10001/a1": {"time": "2026-10-10T10:00:00", "cc": 3, "title": "第一题"},
             "10001/a2": {"time": "2026-10-11T10:00:00", "cc": 0, "title": "第二题",
                          "deleted": True}}
b13 = new_bot(s13)
text, kb = b13._archive_page("10001", 1)
labels = [b.text for row in kb.inline_keyboard for b in row]
check("清单按时间从新到旧 (新的在上面)", "第二题" in labels[0] and "第一题" in labels[1])
check("清单标出删除和评论数", "🚫" in labels[0] and "💬3" in labels[1])
check("清单按钮指向话题卡片", "T:10001:a2:L:1" in callbacks(kb))

# 老档案没有 title: 从 raw 里读
s14 = fake_sentinel()
write_raw(s14, "10001", "b1", {"topic_id": "b1", "title": "老档案的标题"})
s14.state = {"10001/b1": {"time": "2026-10-09T10:00:00"}}
b14 = new_bot(s14)
text, kb = b14._archive_page("10001", 1)
check("老档案的标题从 raw 里补",
      any("老档案的标题" in b.text for row in kb.inline_keyboard for b in row))

# 分页
s15 = fake_sentinel()
s15.state = {"10001/p%02d" % i: {"time": "2026-10-%02dT10:00:00" % (i + 1),
                                 "title": "题%d" % i, "cc": 0} for i in range(14)}
b15 = new_bot(s15)
t1, k1 = b15._archive_page("10001", 1)
t2, k2 = b15._archive_page("10001", 2)
check("清单每页 6 条", len([c for c in callbacks(k1) if c.startswith("T:")]) == 6)
check("翻页回调带页码", "L:10001:2" in callbacks(k1) and "L:10001:1" in callbacks(k2))
check("页码越界会夹住", b15._archive_page("10001", 99)[0].count("第 3/3 页"))

# 跨星球
s16 = fake_sentinel()
s16.gnames = {"10001": "星球甲", "10002": "星球乙"}
s16.state = {"10001/x": {"time": "2026-10-10T10:00:00", "title": "甲的题"},
             "10002/y": {"time": "2026-10-12T10:00:00", "title": "乙的题"}}
b16 = new_bot(s16)
text, kb = b16._all_page(1)
check("跨星球清单带星球名",
      any("星球乙" in b.text for row in kb.inline_keyboard for b in row)
      and "T:10002:y:A:1" in callbacks(kb))

# ---------- 16 话题卡片 ----------
s17 = fake_sentinel()
qa = {"topic_id": "q1", "create_time": "2026-10-14T11:07:00", "title": "问答标题",
      "type": "q&a", "digested": True, "comments_count": 2,
      "question": {"text": "问题正文", "owner": {"name": "读者"}},
      "answer": {"text": "回答正文", "owner": {"name": "星主"}}}
write_raw(s17, "10001", "q1", qa, [comment(1, "张三", "来了")])
s17.state = {"10001/q1": {"time": qa["create_time"], "cc": 2, "title": "问答标题"}}
za.save_json(os.path.join(s17.out, "raw", "10001", "q1__rev_1.json"), {"topic": qa})
b17 = new_bot(s17)
text, kb = b17._card_view("10001", "q1", "L", 2)
check("卡片有标题作者和计数", "问答标题" in text and "星主" in text and "💬 2" in text)
check("卡片标出精华与改动", "精华" in text and "改过 1 次" in text)
check("卡片入口齐了", {"F:10001:q1", "C:10001:q1:1", "V:10001:q1:0",
                    "U:10001:q1:L:2", "L:10001:2"} <= set(callbacks(kb)))
check("卡片说清本地和服务器差多少", "服务器有 2 条评论" in text)

# 存档丢了的兜底
s18 = fake_sentinel()
s18.state = {"10001/z": {"time": "2026-10-14T11:07:00", "cc": 0, "title": "没存档"}}
b18 = new_bot(s18)
text, kb = b18._card_view("10001", "z", "L", 1)
check("存档丢了也能从这里捞回来", "存档文件不在了" in text
      and "U:10001:z:L:1" in callbacks(kb))

# ---------- 17 评论页 ----------
s19 = fake_sentinel()
many = [comment(i, "用户%d" % i, "评论内容%d" % i, "2026-10-15T09:%02d:00" % i,
                replies=[comment(100 + i, "回复者%d" % i, "子回复%d" % i)]
                if i == 1 else None)
        for i in range(1, 8)]
write_raw(s19, "10001", "c1", {"topic_id": "c1", "title": "评论很多的题"}, many)
s19.state = {"10001/c1": {"time": "2026-10-15T08:00:00", "cc": 7, "title": "评论很多的题"}}
b19 = new_bot(s19)
text, kb = b19._comments_view("10001", "c1", 1)
check("评论页分页", "第 1/2 页" in text and len(many) == 7)
check("评论页显示楼中楼", "回复者1" in text and "回复" in text)
check("评论页有翻页和拉最新", {"C:10001:c1:2", "Z:10001:c1", "T:10001:c1:L:1"} <= set(callbacks(kb)))
text2, _ = b19._comments_view("10001", "c1", 0)
check("第 0 页就是最后一页", "第 2/2 页" in text2)

# 从服务器补评论 (async)
s20 = fake_sentinel()
write_raw(s20, "10001", "c2", {"topic_id": "c2", "title": "补评论"},
          [comment(1, "张三", "老评论")])
s20.state = {"10001/c2": {"time": "2026-10-15T08:00:00", "cc": 1, "title": "补评论"}}


class MoreCli:
    def comments(self, tid, limit=0, begin=None):
        return [comment(1, "张三", "老评论"), comment(2, "李四", "新评论")]


s20.cli = MoreCli()
b20 = new_bot(s20)
text, kb = asyncio.run(b20._fetch_comments_and_show("10001", "c2"))
check("补评论后告警补了几条", "刚补上 1 条评论" in text and "新评论" in text)
check("补完的评论落盘了", len(s20.archive.load("10001", "c2")["comments"]) == 2)

# ---------- 18 改动对比页 ----------
s21 = fake_sentinel()
za.save_json(os.path.join(s21.out, "raw", "10001", "r1.json"),
             {"topic": {"topic_id": "r1", "title": "标题", "talk": {"text": "旧的\n留着"}}})
za.save_json(os.path.join(s21.out, "raw", "10001", "r1__rev_1.json"),
             {"topic": {"topic_id": "r1", "title": "标题", "talk": {"text": "新的\n留着"}},
              "_fetched_at": "2026-10-19 08:00:00"})
s21.state = {"10001/r1": {"time": "2026-10-15T08:00:00", "cc": 0, "title": "标题", "revs": 1}}
b21 = new_bot(s21)
text, kb = b21._rev_view("10001", "r1", 0)
check("对比页标出新增删除行", "新增 1 行" in text and "删掉 1 行" in text)
check("对比页列出改了哪行", "－ 旧的" in text and "＋ 新的" in text)
check("对比页说明比的是最初版", "最初备份" in text)
text0, _ = new_bot(fake_sentinel())._rev_view("10001", "nope", 0)
check("没改动时说清楚", "还没有改动记录" in text0)

# ---------- 19 全文页 ----------
s22 = fake_sentinel()
qa2 = dict(qa, topic_id="q2", conversation=None,
           task={"text": "作业正文", "owner": {"name": "甲"}},
           talk={"text": "附带说明", "files": [{"file_id": "f1", "name": "附件.pdf",
                                                "size": 2048}]})
write_raw(s22, "10001", "q2", qa2)
s22.state = {"10001/q2": {"time": qa2["create_time"], "cc": 0, "title": "问答标题"}}
b22 = new_bot(s22)
parts = b22._full_texts("10001", "q2")
whole = "\n".join(parts)
check("全文分区块", "【提问】" in whole and "【回答】" in whole and "【作业】" in whole)
check("全文带原文链接", "wx.zsxq.com/dweb2/index/topic/q2" in whole)
check("全文列出附件", "附件.pdf" in whole and "2.0 KB" in whole)
check("全文每条都不超上限", all(len(p) <= tg_bot.MAXLEN for p in parts))

# ---------- 20 搜索 ----------
s23 = fake_sentinel()
s23.state = {"10001/s1": {"time": "2026-10-10T10:00:00", "title": "聊聊房价"},
             "10001/s2": {"time": "2026-10-11T10:00:00", "title": "聊聊教育"}}
b23 = new_bot(s23)
hits = s23.archive.search(s23.state, "房价")
check("按标题搜得到", len(hits) == 1 and hits[0]["tid"] == "s1")
check("搜不到就是空", s23.archive.search(s23.state, "不存在") == [])
text, kb = b23._list_view(hits, 1, "🔎 标题含「房价」", origin="A", gid=None,
                          token="f1", with_group=True)
check("搜索结果带星球名和卡片入口",
      any("星球甲" in b.text for row in kb.inline_keyboard for b in row)
      and "T:10001:s1:A:1" in callbacks(kb))

# ---------- 21 归档时把标题记进 state ----------
s24 = fake_sentinel()


class OneTopicCli:
    def req(self, path, retries=3):
        return {"succeeded": False}

    def comments(self, tid, limit=0, begin=None):
        return []


s24.cli = OneTopicCli()
os.makedirs(os.path.join(s24.out, "raw", "10001"), exist_ok=True)
s24.archive_topic = w.Sentinel.archive_topic.__get__(s24)
s24.archive_topic("10001", "星球甲",
                  {"topic_id": "n1", "create_time": "2026-10-15T10:00:00",
                   "title": "新归档的标题", "talk": {"text": "正文"}})
check("归档时记下标题", s24.state["10001/n1"]["title"] == "新归档的标题")
check("state 里也记了评论数和修改时间",
      "cc" in s24.state["10001/n1"] and "mtime" in s24.state["10001/n1"])
check("刚归档的话题不会立刻被抽查", s24.state["10001/n1"].get("swept"))

# ---------- 22 生成的 HTML 必须能被 Telegram 接受 ----------
s25 = fake_sentinel()
nasty = {"topic_id": "h1", "create_time": "2026-10-15T10:00:00",
         "title": "带 <b>尖括号</b> 的标题",
         "talk": {"text": "正文里有 < 号和 <e type=\"web\" href=\"https%3A%2F%2Fe.com\" "
                          "title=\"%E9%93%BE%E6%8E%A5\"/> 链接", "owner": {"name": "<甲>"}}}
write_raw(s25, "10001", "h1", nasty,
          [comment(1, "<张三>", "评论 <b>别当成标签</b> & 符号"),
           comment(2, "李四", "回复一下", replies=[
               comment(3, "王五", "楼中楼 <i>x</i>")])])
s25.state = {"10001/h1": {"time": nasty["create_time"], "cc": 2, "title": "带 <b>尖括号</b> 的标题",
                          "revs": 1}}
za.save_json(os.path.join(s25.out, "raw", "10001", "h1__rev_1.json"),
             {"topic": dict(nasty, talk={"text": "改过的正文 < 号", "owner": {"name": "甲"}}),
              "_fetched_at": "2026-10-19 08:00:00"})
b25 = new_bot(s25)
card_text, _ = b25._card_view("10001", "h1", "L", 1)
com_text, _ = b25._comments_view("10001", "h1", 1)
rev_text, _ = b25._rev_view("10001", "h1", 1)
check("卡片 HTML 合法", html_ok(card_text))
check("评论页 HTML 合法 (含尖括号和楼中楼)", html_ok(com_text) and "&lt;张三&gt;" in com_text)
check("对比页 HTML 合法", html_ok(rev_text))
check("全文 HTML 合法", all(html_ok(p) for p in b25._full_texts("10001", "h1")))
check("推送文本 HTML 合法",
      html_ok(tg_bot._fmt_topic_brief(nasty, "<星球>"))
      and html_ok(tg_bot._fmt_topic_brief(qa, "星球甲")))

# ---------- 23 老配置升级 ----------
legacy = os.path.join(TMP, "legacy.json")
with open(legacy, "w", encoding="utf-8") as f:
    json.dump({"groups": ["10001"], "notify_comments": False,
               "group_opts": {"10001": {"notify_comments": True}}}, f)
cfg = w.load_cfg(legacy, None)
check("老的 notify_comments=false 换成「全关」", cfg["comment_alert"] == "off")
check("星球里老的 true 换成「都提醒」",
      cfg["group_opts"]["10001"].get("comment_alert") == "both"
      and "notify_comments" not in cfg["group_opts"]["10001"])
check("老键不再留在运行时配置里", "notify_comments" not in cfg)

shutil.rmtree(TMP, ignore_errors=True)
print("\n全部通过 (%d 项)" % ok)
