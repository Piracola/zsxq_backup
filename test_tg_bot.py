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

shutil.rmtree(TMP, ignore_errors=True)
print("\n全部通过 (%d 项)" % ok)
