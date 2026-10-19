#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_tg_bot.py — 机器人离线自检 (不联网, 不需要凭证)

跑法:
  python3 -X utf8 test_tg_bot.py

覆盖:
  1 话题格式化: 问答全文 / 可折叠引用 / 链接与加粗转 HTML / 超长截断补闭合标签
  2 鉴权守卫: 白名单内放行, 白名单外拦截并回显对方 ID
  3 按钮键盘: 星球列表标记 / 单星球菜单
  4 自动推送: 开启时把星球补进盯守列表并落盘
  5 保留条数: 循环切换并真实清理旧文件
  6 抓取设置: 逐星球覆盖 / 落盘 / 状态里读得回来
  7 投递链路: 哨兵线程 → 机器人事件循环 (重构后最容易断的一根线)
  8 检测并入抓取轮: 列表里的老话题零请求比对 / 窗口内缺席才确认 / 掉出窗口不碰
  9 限流与凭证失效: 都不许把话题误判成被删除
  10 凭证自检缓存: 缓存期内不重复查 / 请求失败或过期才真查
  11 /groups: 名单没填上时当场拉 / 拉不到给出凭证提示
  12 问答图片: 提问/回答图片分组, 最新推送带图
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

# Windows + 代理软件常把 "[::1]" 写进 NO_PROXY, httpx 解析不了会直接崩
if "[::1]" in (os.environ.get("NO_PROXY") or ""):
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import telegram
from telegram.ext import ApplicationHandlerStop

import tg_bot
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
    s.cfg = dict(w.DEFAULT_CFG)
    s.cfg.update(cfg)
    s.notified = []
    s.notify = lambda title, text="": s.notified.append(title)
    s.save_state = lambda: None
    s._auth_fail_ts = s._last_auth_ok = 0.0
    s._auth_dirty = False
    s._last_prune = 0.0
    s.bot = None
    s.archive_topic = lambda gid, gname, topic: s.state.__setitem__(
        "%s/%s" % (gid, topic.get("topic_id")),
        {"time": topic.get("create_time"), "mtime": topic.get("modify_time") or "",
         "cc": topic.get("comments_count") or 0})
    for n in ("opt", "set_opt", "in_quiet", "latest_page", "poll_group", "check_window",
              "on_detail", "on_deleted", "auth_ok", "describe", "autopush_on"):
        setattr(s, n, getattr(w.Sentinel, n).__get__(s))
    s.topic_link = w.Sentinel.topic_link       # staticmethod, 不要绑到实例上
    return s


s = FakeSentinel()
s.gnames = {"10001": "星球甲", "10002": "星球乙"}
s.cfg = {"groups": ["10001"], "group_keep": {}, "_config_path": os.path.join(TMP, "watch.json"),
         "notify": {"telegram": "123456:FAKE-TOKEN"}, "bot_users": ["2013037643"]}
s.state = {"10001/%d" % i: {"time": "2026-07-%02dT10:00:00" % i,
                            "file": os.path.join("g", "t%d.md" % i)} for i in range(1, 60)}
s.out, s.state_path = TMP, os.path.join(TMP, "state.json")
s.cred_path, s._last_auth_ok = os.path.join(TMP, "cred.json"), time.time()
for n in ("save_state", "rebuild_index", "prune_group"):
    setattr(s, n, getattr(w.Sentinel, n).__get__(s))
s.latest_page = lambda gid, count=10: []
s.hot_reload_cred = lambda: True

bot = tg_bot.TgBot(s)

# 1 话题格式化
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
long_one = tg_bot._clamp(tg_bot._fmt_topic_brief(
    {"topic_id": "9", "create_time": "2026-01-01T00:00:00",
     "talk": {"text": "x" * 6000, "owner": {"name": "a"}}}, "g"))
check("超长截断补闭合标签", len(long_one) <= tg_bot.MAXLEN + len(tg_bot.BQ_CLOSE)
      and long_one.count(tg_bot.BQ_OPEN) == long_one.count(tg_bot.BQ_CLOSE))

# 2 鉴权守卫
def make_update(uid):
    u = telegram.User(id=uid, first_name="X", is_bot=False)
    c = telegram.Chat(id=uid, type="private")
    m = telegram.Message(message_id=1, date=datetime.now(timezone.utc), chat=c,
                         from_user=u, text="/menu")
    return telegram.Update(update_id=uid, message=m)


refused = []
bot._send = lambda chat_id, text, kb=None: _noop(refused.append((chat_id, text)))


async def _noop(_):
    return None


asyncio.run(bot.guard(make_update(2013037643), None))
check("白名单内放行", not refused)
try:
    asyncio.run(bot.guard(make_update(999999), None))
    check("白名单外拦截", False)
except ApplicationHandlerStop:
    check("白名单外拦截并回显 ID", refused and "你的 ID: 999999" in refused[-1][1])

# 3 键盘
keys = [b.callback_data for row in bot.kb_groups().inline_keyboard for b in row]
check("星球列表按钮", {"g:10001", "g:10002"} <= set(keys))
check("单星球菜单", any("最新 10 条" in b.text
                    for row in bot.kb_group("10001").inline_keyboard for b in row))

# 4 自动推送联动
bot.toggle_autopush("10002")
saved = json.load(open(s.cfg["_config_path"], encoding="utf-8"))
check("开启时补进盯守列表并落盘",
      "10002" in s.cfg["groups"] and "10002" in saved["groups"]
      and saved["bot_autopush"]["10002"])

# 5 保留条数清理
gd = os.path.join(TMP, "g")
os.makedirs(gd, exist_ok=True)
for i in range(1, 60):
    open(os.path.join(gd, "t%d.md" % i), "w").write("x")
bot.cycle_keep("10001")
check("只留 50 条并清理旧文件",
      s.cfg["group_keep"]["10001"] == 50 and len(s.state) == 50 and len(os.listdir(gd)) == 50)
saved = json.load(open(s.cfg["_config_path"], encoding="utf-8"))
check("保留条数落盘", saved.get("group_keep", {}).get("10001") == 50)

# 5b 抓取设置: 可以逐个星球改, 改完直接落盘
for n in ("opt", "set_opt", "describe"):
    setattr(s, n, getattr(w.Sentinel, n).__get__(s))
bot.cycle_opt("10001", "poll_interval")
check("间隔可逐星球覆盖", s.opt("10001", "poll_interval") == 45)
bot.cycle_opt("10001", "page_size")
bot.cycle_opt("10001", "max_comments")
saved = json.load(open(s.cfg["_config_path"], encoding="utf-8"))
check("抓取设置逐项落盘", saved["group_opts"]["10001"] ==
      {"poll_interval": 45, "page_size": 30, "max_comments": 20})
check("状态里读得回节奏", "45" in s.describe("10001") and "30 条" in s.describe("10001"))

# 6 投递链路 (哨兵同步线程 → 机器人事件循环)
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
bot.push_topic = lambda topic, gname: pushed.append((topic, gname))
bot.autopush["10001"] = True
sent.poll_group("10001")
check("新话题投递到机器人", len(pushed) == 1
      and "正文" in tg_bot._fmt_topic_brief(pushed[0][0], pushed[0][1]))

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
loop.call_soon_threadsafe(loop.stop)

# 8 检测并入抓取轮: 列表里的老话题零请求比对, 窗口内缺席才单独确认
s2 = fake_sentinel(recent_days=30, check_interval=300, check_batch=10)
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
     "modify_time": "2026-10-19T08:00:00"},
    {"topic_id": "alive", "create_time": "2026-10-10T10:00:00", "comments_count": 3}]


class FakeCli:
    def __init__(self):
        self.hit = []

    def req(self, path, retries=3):
        self.hit.append(path)
        if path.endswith("/vanished"):
            return {"succeeded": False, "code": 1007, "error": "主题不存在或已被删除"}
        return {"succeeded": True, "resp_data": {"topic": {"comments_count": 5}}}

    def comments(self, tid, limit=0):
        return []

    def whoami(self):
        return {"name": "me"}


s2.cli = FakeCli()
s2.poll_group("10001")
check("列表里已有的老话题不发请求", "/v2/topics/alive" not in s2.cli.hit)
check("掉出窗口的老话题根本不碰", "/v2/topics/old" not in s2.cli.hit)
check("窗口内缺席 + 明确报不存在 → 判删除", s2.state["10001/vanished"].get("deleted"))
check("第一次被编辑也能发现", any("变动" in t for t in s2.notified))
check("新话题照常入库", "10001/new" in s2.state)

# 8b 限流/5xx 没有"话题不存在"的明确信号 → 不当成删除
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
      not s5.state["10001/busy"].get("deleted") and not any("删除" in t for t in s5.notified))

# 8 凭证自检缓存: 缓存期内不重复问 whoami, 请求失败或过期才真查
class CntCli:
    def __init__(self):
        self.n = 0

    def whoami(self):
        self.n += 1
        return {"name": "me"}


s3 = FakeSentinel()
s3.cfg = {"auth_cache_ttl": 1800}
s3.cli = CntCli()
s3.notified = []
s3.notify = lambda title, text="": s3.notified.append(title)
s3._auth_fail_ts = s3._last_auth_ok = 0.0
s3._auth_dirty = False
s3.auth_ok = w.Sentinel.auth_ok.__get__(s3)
check("首次凭证自检真查一次", s3.auth_ok() is True and s3.cli.n == 1)
check("缓存期内不重复查", s3.auth_ok() is True and s3.cli.n == 1)
s3._auth_dirty = True
check("请求失败后强制重验", s3.auth_ok() is True and s3.cli.n == 2)
s3._last_auth_ok = time.time() - 99999
check("缓存过期后重验", s3.auth_ok() is True and s3.cli.n == 3)
s3.cli.whoami = lambda: {}
check("凭证真失效时返回 False", s3.auth_ok(force=True) is False)

# 9 凭证失效时不要把话题误判成被删除
s4 = fake_sentinel(recent_days=30, check_interval=300, auth_cache_ttl=0)
s4.state = {"10001/ddd": {"time": "2026-10-12T10:00:00", "mtime": "", "cc": 1}}
s4.latest_page = lambda gid, count=None: [
    {"topic_id": "other", "create_time": "2026-10-10T10:00:00", "comments_count": 1}]


class FakeCliDead:
    def req(self, path, retries=3):
        return {"succeeded": False, "error": "HTTP 401"}

    def comments(self, tid, limit=0):
        return []

    def whoami(self):
        return {}


s4.cli = FakeCliDead()
s4.poll_group("10001")
check("凭证失效时不判定删除", not s4.state["10001/ddd"].get("deleted"))
check("只发凭证失效告警, 不发删除告警",
      any("凭证" in t for t in s4.notified) and not any("删除" in t for t in s4.notified))

# 10 /groups: 名单是抓取循环顺手填的, 还没填上要当场拉, 拉不到要说清原因
class GroupCli:
    def __init__(self):
        self.n = 0

    def groups(self):
        self.n += 1
        return [{"group_id": "10001", "name": "星球甲"}]


s6 = fake_sentinel()
s6.gnames = {}                       # 刚装好、循环还没跑起来的样子
for n in ("refresh_groups", "ensure_groups"):
    setattr(s6, n, getattr(w.Sentinel, n).__get__(s6))
s6.cli = GroupCli()
b6 = tg_bot.TgBot.__new__(tg_bot.TgBot)
b6.s = s6
txt = asyncio.run(b6.groups_text())
check("名单为空时当场拉一次",
      s6.cli.n == 1 and s6.gnames == {"10001": "星球甲"} and "你加入的星球 (1)" in txt)
asyncio.run(b6.groups_text())
check("名单已有就不重复拉", s6.cli.n == 1)


class NoGroupCli:
    def groups(self):
        return []


s7 = fake_sentinel()
s7.gnames = {}
for n in ("refresh_groups", "ensure_groups"):
    setattr(s7, n, getattr(w.Sentinel, n).__get__(s7))
s7.cli = NoGroupCli()
b7 = tg_bot.TgBot.__new__(tg_bot.TgBot)
b7.s = s7
check("拉不到时给出凭证提示", asyncio.run(b7.groups_text()) == tg_bot.GROUPS_EMPTY)
check("拉不到时按钮回到主菜单", b7.kb_groups().inline_keyboard[0][0].callback_data == "groups")

# 11 问答图片: 提问图片在 question.images, 和 talk.images 不是同一个字段
qa = {"topic_id": "888", "create_time": "2026-10-15T11:00:00", "title": "提问预览",
      "question": {"text": "问题", "owner": {"name": "读者"},
                   "images": [{"large": {"url": "https://img.example/q1.jpg"}},
                              {"large": {"url": "https://img.example/q2.jpg"}}]},
      "answer": {"text": "回答", "owner": {"name": "星主"},
                 "images": [{"original": {"url": "https://img.example/a1.jpg"}}]}}
qa_groups = tg_bot._topic_image_groups(qa)
check("问答图片按提问/回答分组",
      [g[0] for g in qa_groups] == ["提问附图", "回答附图"]
      and qa_groups[0][1] == ["https://img.example/q1.jpg", "https://img.example/q2.jpg"]
      and qa_groups[1][1] == ["https://img.example/a1.jpg"])

sent_pics = []


async def _keep_text(chat_id, text, kb=None):
    return None


async def _keep_pics(chat_id, groups):
    sent_pics.append(groups)


bot._send, bot._send_images = _keep_text, _keep_pics
s.latest_page = lambda gid, count=10: [qa]
asyncio.run(bot.push_latest(555, "10001", 1))
check("最新话题推送带问答图片", sent_pics and sent_pics[0] == qa_groups)

shutil.rmtree(TMP, ignore_errors=True)
print("\n全部通过 (%d 项)" % ok)
