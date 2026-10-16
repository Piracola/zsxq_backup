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
  6 投递链路: 哨兵线程 → 机器人事件循环 (重构后最容易断的一根线)
  7 复查节奏: 未到周期不查 / 到期才查 / 拉取失败当轮二次确认 / 判定即告警
  8 凭证自检缓存: 缓存期内不重复查 / 请求失败或过期才真查
  9 凭证失效保护: 不把话题误判成被删除, 只发凭证告警
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

# 6 投递链路 (哨兵同步线程 → 机器人事件循环)
sent_cfg = dict(w.DEFAULT_CFG)
sent_cfg.update({"_config_path": os.path.join(TMP, "w2.json"), "groups": ["10001"],
                 "notify": {"telegram": "123456:FAKE-TOKEN"}, "bot_users": ["2013037643"]})
json.dump({"access_token": "X", "aduid": "Y"}, open(os.path.join(TMP, "cred2.json"), "w"))
sent = w.Sentinel(os.path.join(TMP, "cred2.json"), TMP, sent_cfg)
sent.gnames = {"10001": "星球甲"}
sent.auth_ok = lambda: True
sent.latest_page = lambda gid, count=20: [
    {"topic_id": "777", "create_time": "2026-10-15T10:00:00",
     "talk": {"text": "正文", "owner": {"name": "甲"}}}]
sent.archive_topic = lambda gid, gname, topic: sent.state.__setitem__(
    "%s/%s" % (gid, topic["topic_id"]), {"time": topic["create_time"]})
sent.rebuild_index = lambda: None

got = []
sent.bot = bot
bot.push_text = lambda text: got.append(text)
bot.autopush["10001"] = True
sent.poll_new()
check("新话题投递到机器人", len(got) == 1 and "正文" in got[0])

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

# 7 复查节奏: 到周期才查, 拉取失败当轮二次确认
s2 = FakeSentinel()
s2.out = os.path.join(TMP, "chk")
os.makedirs(s2.out, exist_ok=True)
s2.gnames = {"10001": "星球甲"}
s2.cfg = {"check_archived": True, "recent_days": 7, "check_interval": 86400,
          "check_batch": 10, "verify_gap": 0, "confirm_gap": 150, "api_sleep": 1.0}
fresh = time.strftime("%Y-%m-%dT%H:%M:%S")
nowt = time.time()
s2.state = {
    "10001/aaa": {"time": fresh, "last_check": nowt},          # 刚查过 → 应跳过
    "10001/bbb": {"time": fresh, "last_check": nowt - 90000},  # 已过复查周期 → 应查
    "10001/ccc": {"time": fresh},                              # 从未查过 → 应查
}
hit = []


class FakeCli:
    def req(self, path, retries=3):
        hit.append(path)
        return {"succeeded": False, "error": "HTTP 403"}


s2.cli = FakeCli()
s2.cli.whoami = lambda: {"name": "me"}        # 凭证是好的 → 证明"两次拉不到"才是删除
s2.notified = []
s2.notify = lambda title, text="": s2.notified.append(title)
s2.save_state = lambda: None
s2._auth_fail_ts = s2._last_auth_ok = 0.0
s2._auth_dirty = False
for n in ("recent_keys", "check_topics", "on_missing", "auth_ok"):
    setattr(s2, n, getattr(w.Sentinel, n).__get__(s2))
s2.topic_link = w.Sentinel.topic_link          # staticmethod, 不要绑到实例上
s2.check_topics()
check("未到复查周期的话题不碰", not any(p.endswith("/aaa") for p in hit))
check("到期话题同轮拉两次后定性为删除",
      sorted(hit) == ["/v2/topics/bbb"] * 2 + ["/v2/topics/ccc"] * 2
      and s2.state["10001/bbb"].get("deleted") and s2.state["10001/ccc"].get("deleted"))
check("判定删除即推送通知", len(s2.notified) == 2)

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
s4 = FakeSentinel()
s4.out = os.path.join(TMP, "deadcred")
os.makedirs(s4.out, exist_ok=True)
s4.gnames = {"10001": "星球甲"}
s4.cfg = {"check_archived": True, "recent_days": 7, "check_interval": 600,
          "check_batch": 10, "verify_gap": 0, "confirm_gap": 150, "auth_cache_ttl": 0}
s4.state = {"10001/ddd": {"time": fresh}}
s4.cli = FakeCli()
s4.cli.whoami = lambda: {}
s4.notified = []
s4.notify = lambda title, text="": s4.notified.append(title)
s4.save_state = lambda: None
s4._auth_fail_ts = s4._last_auth_ok = 0.0
s4._auth_dirty = False
for n in ("recent_keys", "check_topics", "on_missing", "auth_ok"):
    setattr(s4, n, getattr(w.Sentinel, n).__get__(s4))
s4.topic_link = w.Sentinel.topic_link
s4.check_topics()
check("凭证失效时不判定删除", not s4.state["10001/ddd"].get("deleted"))
check("只发凭证失效告警, 不发删除告警",
      any("凭证" in t for t in s4.notified) and not any("删除" in t for t in s4.notified))

shutil.rmtree(TMP, ignore_errors=True)
print("\n全部通过 (%d 项)" % ok)
