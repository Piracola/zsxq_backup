#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zsxq_watch.py — 知识星球云端哨兵

  * 按各星球自己的间隔看一眼最新话题, 发现新话题立即全量备份
    (raw JSON 先落盘 → 图片/附件 → Markdown), 内容被删之前已经拿回本地。
  * 复查搭同一趟车: 列表里每条已归档话题都当场比对修改时间与评论数, 不多发请求。
    只有"本轮窗口内本该出现、却没出现"的话题才单独确认一次:
      - 确认被删  → 快照写入 deleted/ 并发 IM 告警
      - 内容被编辑 → 修订版另存 raw/gid/tid__rev_N.json, 发告警 + 逐行差异
      - 评论数变化 → 拉一次评论, 跟本地基线比出新增/消失的那几条, 连同是谁说的
                     一起告警 (comment_alert 分四档: 都提醒 / 只看新增 / 只看减少 / 全关)
  * 评论抽查: 每轮顺手把一个话题的评论区整段翻一遍 (默认 1 条), 兜住那个计数看不见的
    变化 —— 楼中楼被删 (平台数字常常不跟着减)、一轮之内涨了又跌回原数、增量拉漏的回复。
    注意 comments_count 数的是「主评论 + 楼中楼」, 有人回复楼中楼数字是会动的。
  * 抓取节奏逐个星球可调: 间隔、每轮页数、复查周期与窗口、评论上限、是否下图、
    夜间静默、评论提醒档位、评论抽查密度。Telegram 机器人的「抓取设置」里点按钮就能改。
  * 凭证失效自动告警 (6 小时冷却), 凭证本身仍需手机端提取:
      python zsxq_token.py --json cred.json
  * IM 告警渠道: 企业微信机器人 / 钉钉机器人 / Telegram / Bark / 通用 webhook

用法:
  python zsxq_watch.py --cred cred.json --group 12345678901234 --out zsxq_backup
  python zsxq_watch.py --once          # 只跑一轮 (配合 cron 也行)
配置写在 watch.json (首次运行自动生成模板), 告警渠道全在里面。
"""
import argparse, json, os, random, sys, time, urllib.parse, urllib.request
from collections import deque
from zsxq_archive import (Client, download, collect_images, collect_files,
                          render_md, topic_filename, safe_name, save_json,
                          topic_gone, topic_title, log)
from tg_topics import Archive
from tg_bot import TgBot


DEFAULT_CFG = {
    "groups": [],              # 要盯的星球 ID 列表; 命令行 --group 优先
    # ---- 抓取节奏: 下面是全局默认值, 单个星球可以在 group_opts 里单独覆盖 ----
    "poll_interval": 30,       # 抓新话题的间隔 (秒); 实际会加 ±25% 抖动, 避免请求过于规律
    "page_size": 20,           # 每轮拉多少条 (接口实测上限 30, 再多直接报"无效的count")
    "check_interval": 600,     # 窗口内缺席的话题, 同一话题两次确认的最小间隔 (秒); 0 = 不做确认
    "check_batch": 10,         # 每轮最多确认几条, 免得一次性扎堆
    "recent_days": 3,          # 确认范围上限: 只确认最近 N 天的话题
    "max_comments": 0,         # 每个话题最多抓多少条评论; 0 = 全部
    "sweep_interval": 1800,    # 评论抽查: 同一话题两次整段翻评论区的最小间隔 (秒); 0 = 不抽查
    "sweep_batch": 1,          # 每轮最多抽查几条话题的评论区
    "keep_media": True,        # 是否下载图片与附件; False 只留 raw JSON 里的远程链接
    "comment_alert": "both",   # 评论提醒: both 都报 / added 只报新增 / removed 只报减少 / off 全关
    "quiet_hours": [0, 0],     # 夜间静默 [起, 止] 本地小时, 支持跨零点 [22,6]; 起=止 表示不静默
    "notify_new": False,       # 新话题是否也发通知
    "auth_cache_ttl": 1800,    # 凭证自检结果复用秒数(省掉每轮一次 whoami); 0 = 每轮都查
    "bot_users": [],           # Telegram 机器人允许的 Telegram 用户 ID (数字); 空则允许所有私聊用户
    "group_keep": {},          # gid -> 只保留最近 N 条 (机器人里设置); 0/缺省 = 不限制
    "group_opts": {},          # gid -> {"poll_interval": 15, "page_size": 30, ...} 单星球覆盖
    "notify": {
        "wecom": "",           # 企业微信机器人: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
        "dingtalk": "",        # 钉钉机器人: https://oapi.dingtalk.com/robot/send?access_token=xxx
        "telegram": "",        # bot token, 如 123456:ABC-DEF...
        "telegram_chat_id": "",
        "bark": "",            # 如 https://api.day.app/你的key
        "generic": ""          # 任意 HTTP POST, 收到 {"title":..., "text":...}
    }
}

# 老版本的独立复查配置, 检测并入每轮抓取后不再使用
RETIRED = ("check_archived", "verify_gap", "confirm_gap")

ALERT_MODES = ("both", "added", "removed", "off")


def _alert_mode(v):
    """评论提醒的取值。新写法是四档字符串, 老写法是 True/False, 两种都收。"""
    if isinstance(v, bool):
        return "both" if v else "off"
    s = str(v or "").strip().lower()
    return s if s in ALERT_MODES else "both"


def _ts(s):
    """create_time → 秒; 解析不了就当 0, 不要让它抛异常。"""
    try:
        return time.mktime(time.strptime((s or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0.0


def load_cfg(path, cli_groups):
    cfg = dict(DEFAULT_CFG)
    # 这两项会被 set_opt / cycle_keep 写进去, 先各复制一份, 别跟 DEFAULT_CFG 共用同一个 dict
    cfg["group_opts"] = {}
    cfg["group_keep"] = {}
    if os.path.exists(path):
        try:
            user = json.load(open(path, encoding="utf-8"))
            cfg.update(user)
            cfg["notify"] = dict(DEFAULT_CFG["notify"])
            cfg["notify"].update(user.get("notify") or {})
            # notify_comments (True/False) 换成 comment_alert 四档; 按原来的意思翻译过来
            if "notify_comments" in cfg:
                old = _alert_mode(cfg.pop("notify_comments"))
                if "comment_alert" not in user:
                    cfg["comment_alert"] = old
                    log("[·] notify_comments 换成四档的 comment_alert 了 "
                        "(both/added/removed/off), 这次按 %s 算" % old)
            cfg["comment_alert"] = _alert_mode(cfg.get("comment_alert"))
            for o in (cfg.get("group_opts") or {}).values():
                if not isinstance(o, dict):
                    continue
                if "notify_comments" in o:
                    o.setdefault("comment_alert", _alert_mode(o.pop("notify_comments")))
                if "comment_alert" in o:
                    o["comment_alert"] = _alert_mode(o["comment_alert"])
            gone = [k for k in RETIRED if k in user]
            if gone:
                log("[·] 这些配置项已停用, 可以从 watch.json 删掉: %s (检测已并入每轮抓取)" %
                    ", ".join(gone))
        except Exception as e:
            log("[!] 配置文件解析失败, 使用默认配置: %s" % e)
    else:
        save_json(path, cfg, indent=1)
        log("[+] 已生成配置模板 %s — 告警渠道请填进里面的 notify 字段" % path)
    if cli_groups:
        cfg["groups"] = cli_groups
    return cfg


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
        self.archive = Archive(out)       # 备份目录的视图, 机器人也用这个
        self.events = deque(maxlen=200)   # 最近变动 (只在内存里, 机器人「最近变动」页读它)
        self.last_poll = {}               # gid -> 上次成功看过列表的时间
        self.gnames = {}          # gid -> 名称
        self._groups_at = 0.0     # 上次拉星球名单的时间
        self._auth_fail_ts = 0.0  # 凭证告警冷却
        self._last_auth_ok = 0.0  # 最近一次凭证校验通过的时间
        self._auth_dirty = False  # 有请求失败 → 下一轮强制重验凭证, 不吃缓存
        self._last_prune = 0.0    # 限量清理不必每轮都做
        self.bot = None           # Telegram 机器人线程 (main 里启动)

    # ---------- 基础 ----------

    def opt(self, gid, key):
        """取一个抓取设置: 星球自己的覆盖 > 全局值 > 内置默认值。"""
        o = (self.cfg.get("group_opts") or {}).get(str(gid)) or {}
        if key in o:
            return o[key]
        return self.cfg.get(key, DEFAULT_CFG.get(key))

    def set_opt(self, gid, key, value):
        """写回星球自己的设置; 跟全局值一样就删掉这条覆盖, 配置文件保持干净。"""
        go = self.cfg.setdefault("group_opts", {})
        d = go.setdefault(str(gid), {})
        if value == self.cfg.get(key, DEFAULT_CFG.get(key)):
            d.pop(key, None)
        else:
            d[key] = value
        if not d:
            go.pop(str(gid), None)

    def in_quiet(self, gid=None):
        """现在是不是静默时段。全局与星球各自可以设。"""
        q = self.opt(gid, "quiet_hours") if gid else self.cfg.get("quiet_hours")
        qs, qe = (list(q or [0, 0]) + [0, 0])[:2]
        if qs == qe:
            return False
        h = time.localtime().tm_hour
        return (qs <= h < qe) if qs < qe else (h >= qs or h < qe)

    def save_state(self):
        save_json(self.state_path, self.state, indent=1)

    def add_event(self, kind, gid, tid, title, note=""):
        """记一笔变动, 给机器人的「最近变动」页看。只在内存里, 重启就清空。"""
        self.events.append({"t": time.time(), "kind": kind, "gid": str(gid),
                            "tid": str(tid), "title": (title or "")[:80],
                            "note": (note or "")[:80]})

    def auth_ok(self, force=False):
        """凭证自检。auth_cache_ttl 秒内直接复用上次结果, 不再每轮都问 whoami。
        任何数据请求失败会把 _auth_dirty 置位, 此时不吃缓存、立刻真查一次。"""
        ttl = max(0, int(self.cfg.get("auth_cache_ttl", 1800)))
        if not force and not self._auth_dirty and ttl and \
                (time.time() - self._last_auth_ok) < ttl:
            return True
        self._auth_dirty = False
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
        """拉一遍账号里的星球。拉不到就保留原来的名单, 别因此把满名单清成空的。"""
        got = {str(g.get("group_id")): (g.get("name") or "")
               for g in self.cli.groups() if g.get("group_id")}
        if got:
            self.gnames = got
        return bool(got)

    def ensure_groups(self, force=False):
        """星球名单是抓取循环顺手填的, 机器人按钮也可能要用, 所以这里给个 TTL:
        十分钟内不重复拉。返回名单是否可用。"""
        if not force and self.gnames and time.time() - self._groups_at < 600:
            return True
        try:
            ok = self.refresh_groups()
        except Exception as e:
            log("[!] 拉星球列表失败: %r" % e)
            return False
        if ok:
            self._groups_at = time.time()
        return ok

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
                self._groups_at = 0.0        # 换了凭证, 星球名单重新拉一遍
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
            rev = 1
            while os.path.exists(self.archive.rev_path(gid, tid, rev)):   # 修订快照一起清
                try:
                    os.remove(self.archive.rev_path(gid, tid, rev))
                except OSError:
                    pass
                rev += 1
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

    def notify(self, title, text="", skip_telegram=False):
        """IM 告警。skip_telegram: 机器人已经用带按钮的消息推过了, 别重复发一条。"""
        n = self.cfg.get("notify") or {}
        body = (title + "\n" + text).strip()
        if n.get("wecom"):
            self._post(n["wecom"], {"msgtype": "text",
                                    "text": {"content": body[:2000]}})
        if n.get("dingtalk"):
            self._post(n["dingtalk"], {"msgtype": "markdown", "markdown": {
                "title": title, "text": "## %s\n\n%s" % (title, text)[:18000]}})
        if n.get("telegram") and n.get("telegram_chat_id") and not skip_telegram:
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

    def latest_page(self, gid, count=None):
        """一个星球的最新一页。count 不传就用该星球的 page_size。
        接口实测上限 30, 传 50 直接报"无效的count"。"""
        count = max(1, min(30, int(count or self.opt(gid, "page_size") or 20)))
        d = self.cli.req("/v2/groups/%s/topics?scope=all&count=%d" % (gid, count))
        if not d.get("succeeded"):
            log("    [!] %s 最新列表拉取失败: %s" % (gid, str(d.get("error"))[:80]))
            self._auth_dirty = True      # 拉不动 → 下轮不吃凭证缓存, 真查一次
            return []
        return ((d.get("resp_data") or {}).get("topics")) or []

    def archive_topic(self, gid, gname, topic):
        """全量备份单个话题 (与 zsxq_archive.py 同一套落地结构)。
        评论抓多少条、要不要下图, 按该星球自己的设置来。"""
        tid = str(topic.get("topic_id"))
        gdir = os.path.join(self.out, safe_name(gname, 50) + "_" + gid)
        rawdir = os.path.join(self.out, "raw", gid)
        os.makedirs(gdir, exist_ok=True)
        os.makedirs(rawdir, exist_ok=True)

        limit = int(self.opt(gid, "max_comments") or 0)
        comments = (self.cli.comments(tid, limit=limit)
                    if (topic.get("comments_count") or 0) > 0 else [])
        rawpath = os.path.join(rawdir, "%s.json" % tid)
        payload = {"topic": topic, "comments": comments, "_group": gname,
                   "_fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        save_json(rawpath, payload)

        url2name, file_names = [], []
        if self.opt(gid, "keep_media"):
            for url, base in collect_images(topic) + collect_images(comments):
                if download(url, os.path.join(gdir, "images", base)):
                    url2name.append((url, base))
            for fid, nm, size, dur in collect_files(topic):
                dd = self.cli.req("/v2/files/%s/download_url" % fid)
                u = (dd.get("resp_data") or {}).get("download_url")
                if u and download(u, os.path.join(gdir, "files", nm)):
                    file_names.append((u, nm, size, dur))
        if url2name or file_names:
            payload.update({"_img_map": url2name, "_files": file_names})
            save_json(rawpath, payload)

        fname = topic_filename(topic)
        with open(os.path.join(gdir, fname), "w", encoding="utf-8") as f:
            f.write(render_md(topic, comments, dict(url2name), gname))

        self.state["%s/%s" % (gid, tid)] = {
            "file": os.path.relpath(os.path.join(gdir, fname), self.out),
            "time": topic.get("create_time"),
            "mtime": topic.get("modify_time") or "",
            "cc": topic.get("comments_count") or 0,
            "swept": time.time(),        # 评论刚拉过, 别让抽查立刻又翻一遍
            "title": topic_title(topic)[:80]}
        return rawpath

    def poll_group(self, gid):
        """抓一个星球的最新一页: 新话题备份, 老话题顺手比对, 最后确认窗口内缺席的。
        整轮只有这一个列表请求 —— 复查要的字段列表里本来就带着。"""
        gid = str(gid)
        gname = self.gnames.get(gid, gid)
        page = self.latest_page(gid)
        if not page:
            return
        self.last_poll[gid] = time.time()
        seen, dirty = {}, False
        for topic in page:
            tid = str(topic.get("topic_id"))
            if not tid or tid == "None":
                continue
            seen[tid] = topic
            key = "%s/%s" % (gid, tid)
            if key in self.state:
                dirty = self.on_detail(gid, tid, key, topic) or dirty   # 数据已经在手上
                continue
            log("    [新话题] %s %s" % (tid, (topic.get("title") or "")[:30]))
            self.archive_topic(gid, gname, topic)
            self.save_state()
            self.add_event("new", gid, tid, topic_title(topic), "新话题已备份")
            if self.autopush_on(gid) and self.bot and self.bot.admins_or_any_chat():
                self.bot.push_topic(topic, gname, gid)
            if self.opt(gid, "notify_new"):
                self.notify("📥 新话题已备份",
                            "星球: %s\n标题: %s\n%s" % (
                                gname, (topic.get("title") or "")[:60],
                                self.topic_link(gid, tid)))
        if self.sweep_comments(gid):   # 顺手翻一两条评论区, 补上评论数看不出的变化
            dirty = True
        if dirty:                      # 基线变了才落盘, 别每轮都重写 state.json
            self.save_state()
        self.check_window(gid, page, seen)

    def sweep_comments(self, gid):
        """轮着把评论区整段翻一遍。评论数这个数字看不见的三件事全靠它:
        楼中楼里有人回复、有人删楼中楼, 以及一轮之内涨了又跌回原数。
        每轮最多翻 sweep_batch 条, 同一话题两次之间至少隔 sweep_interval。"""
        every = int(self.opt(gid, "sweep_interval") or 0)
        batch = int(self.opt(gid, "sweep_batch") or 0)
        if every <= 0 or batch <= 0 or self.opt(gid, "comment_alert") == "off":
            return False
        now = time.time()
        days = int(self.opt(gid, "recent_days") or 0)
        cand = []
        for key, v in self.state.items():
            if not key.startswith(gid + "/") or v.get("deleted"):
                continue
            if not (v.get("cc") or 0):
                continue                  # 没评论的话题不用翻; 一旦有人评论, 数量那条路会管
            ct = v.get("time") or ""
            if days and ct and _ts(ct) < now - days * 86400:
                continue
            last = float(v.get("swept") or 0)
            if now - last < every:
                continue
            cand.append((last, key))
        if not cand:
            return False
        cand.sort()                       # 最久没翻过的先翻
        for _, key in cand[:batch]:
            tid = key.split("/", 1)[1]
            self.state[key]["swept"] = now    # 翻了就记下 (失败也记, 免得同一话题连着几轮硬撞)
            try:
                self.sweep_one(gid, tid, key)
            except Exception as e:
                log("[!] 评论抽查出错 %s: %s" % (tid, str(e)[:120]))
        return True

    def sweep_one(self, gid, tid, key):
        """整段拉一遍评论区跟本地比。比对和推送都复用评论变动那一套, 只是触发原因不同。"""
        raw = self.archive.load(gid, tid)
        if not raw:
            return
        v = self.state[key]
        stored = Archive.comments_of(raw)
        limit = int(self.opt(gid, "max_comments") or 0)
        fetched = self.cli.comments(tid, limit=limit)
        if not fetched:
            return
        cc_old = int(v.get("cc") or 0)
        # 口径要跟接口一致: comments_count 是「主评论 + 楼中楼」的总条数
        got = Archive.comment_total(fetched)
        complete = (not limit) or got >= cc_old
        added, gone = Archive.diff_comments(stored, fetched)
        if not complete:
            gone = []                     # 没拉全, 不能说谁不见了
        if not added and not gone:
            log("    [·] 抽查评论区: %s 没有变化" % tid)
            return
        cc = got if complete else max(cc_old, got)
        if added or cc != cc_old:
            self.archive.append_comments(gid, tid, fetched, cc=cc)
        # 数量对齐到刚看到的, 下一轮列表就不会把同一件事再报一遍
        v["cc"] = cc
        mode = self.opt(gid, "comment_alert")
        show_added = [] if mode == "removed" else added
        show_gone = [] if mode == "added" else gone
        if not show_added and not show_gone:
            log("    [·] 抽查评论区: %s 有变化, 但这一档不提醒" % tid)
            return
        title = v.get("title") or tid
        gname = self.gnames.get(gid, gid)
        note = "抽查：多了 %d 条、少了 %d 条（评论数 %d）" % (
            len(show_added), len(show_gone), cc)
        log("    [!] 抽查评论区: %s 新增 %d 条、少了 %d 条%s" % (
            tid, len(show_added), len(show_gone),
            (", 比对不完整" if not complete else "")))
        self.add_event("comment", gid, tid, title, note)
        pushed = bool(self.bot) and self.bot.push_comment_change(
            gid, tid, title, gname, cc, show_added, show_gone, complete)
        lines = [note]
        for c, parent in (show_added + show_gone)[:5]:
            who = ((c.get("owner") or {}).get("name")) or "?"
            body = " ".join((c.get("text") or "").split())[:60] or "(图片)"
            lines.append("· %s: %s" % (who, body))
        self.notify("💬 抽查发现评论有变化", "星球: %s\n标题: %s\n%s\n%s" % (
            gname, title[:60], "\n".join(lines), self.topic_link(gid, tid)),
            skip_telegram=pushed)

    def check_window(self, gid, page, seen):
        """窗口内本该出现、这轮却没出现的话题, 才值得单独发一次请求确认。
        窗口下界是本轮列表里最老那条的时间: 比它更老的话题早就掉出列表,
        不在列表里说明不了任何事, 不能当删除证据。"""
        interval = int(self.opt(gid, "check_interval") or 0)
        if interval <= 0:
            return
        # 置顶话题的发布时间往往很老, 拿它当窗口下界会把整片老话题都算进候选, 所以先剔掉
        times = [t.get("create_time") or "" for t in page if not t.get("sticky")]
        oldest = min(times or [t.get("create_time") or "" for t in page])
        days = int(self.opt(gid, "recent_days") or 0)
        now = time.time()
        cand = []
        for key, v in self.state.items():
            if not key.startswith(gid + "/") or v.get("deleted"):
                continue
            ct = v.get("time") or ""
            if not ct or ct < oldest or key.split("/", 1)[1] in seen:
                continue
            if days and _ts(ct) < now - days * 86400:
                continue
            if now - (v.get("last_check") or 0) < interval:
                continue
            cand.append(key)
        if not cand:
            return
        cand.sort(key=lambda k: self.state[k].get("time") or "", reverse=True)
        budget = max(1, int(self.opt(gid, "check_batch") or 1))
        auth_checked = False
        for key in cand[:budget]:
            tid = key.split("/", 1)[1]
            self.state[key]["last_check"] = now      # 查过就记下, 下轮不重复问
            d = self.cli.req("/v2/topics/%s" % tid)
            if d.get("succeeded"):
                self.on_detail(gid, tid, key,
                               (d.get("resp_data") or {}).get("topic") or {})
                continue
            if topic_gone(d):
                self.on_deleted(gid, key, d)          # 缺席 + 明确报不存在 = 删除
                continue
            # 限流/5xx/网络异常: 既不是删除证据, 也得先排除凭证这个最常见的原因
            if not auth_checked:
                if not self.auth_ok(force=True):
                    self._auth_dirty = True
                    log("    [!] 凭证不可用, 暂停本轮剩余确认: %s" % tid)
                    break
                auth_checked = True
            log("    [?] 确认请求异常, 下轮再看: %s" % tid)
        self.save_state()

    # ---------- 删除 / 篡改监测 ----------

    def autopush_on(self, gid):
        """该星球是否开了机器人自动推送; 没配 admin 时只推给第一个私聊过机器人的用户。"""
        return bool(self.bot and self.bot.autopush.get(str(gid)))

    def on_deleted(self, gid, key, resp):
        """窗口内缺席, 单独确认时接口明确报不存在 → 定性为删除。"""
        tid = key.split("/", 1)[1]
        v = self.state[key]
        title = v.get("title") or ""
        rec = {"topic_id": tid, "group_id": gid, "group": self.gnames.get(gid, gid),
               "title": title, "deleted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "api_resp": resp}
        rawp = os.path.join(self.out, "raw", gid, "%s.json" % tid)
        if os.path.exists(rawp):
            raw = json.load(open(rawp, encoding="utf-8"))
            title = (raw.get("topic") or {}).get("title") or title
            rec["title"] = title
            rec["raw_snapshot"] = raw
        save_json(self.archive.deleted_path(gid, tid), rec, indent=1)
        v["deleted"] = True
        v.pop("gone_seen", None)
        log("    [!] 话题确认被删除: %s %s" % (tid, title[:30]))
        self.add_event("gone", gid, tid, title, "服务器上已删除")
        pushed = bool(self.bot) and self.bot.push_deleted(
            gid, tid, topic_title({"title": title}), self.gnames.get(gid, gid))
        self.notify("🚨 话题被删除 (已备份在手)",
                    "星球: %s\n标题: %s\n备份: %s\n详情: %s" % (
                        rec["group"], rec["title"][:80] or "(无标题)",
                        rawp, self.topic_link(gid, tid)),
                    skip_telegram=pushed)

    def on_detail(self, gid, tid, key, topic):
        """拿一条话题的当前数据比对基线。列表返回的字段与详情接口完全一致,
        所以比对本身不花任何请求; 只有真变了才去抓评论 / 存修订版。
        关心两件事: 内容改没改, 评论数变没变。"""
        v = self.state[key]
        v.pop("gone_seen", None)
        if v.get("deleted"):             # 又被恢复了(或是之前误判): 撤销删除标记
            v.pop("deleted", None)
            log("    [✓] 话题重新可见: %s" % tid)
        mtime = topic.get("modify_time") or ""
        cc = topic.get("comments_count") or 0
        if "mtime" not in v:             # 老档案没有基线, 这轮只把基线记上
            v["mtime"], v["cc"] = mtime, cc
            return True
        old_m, old_cc = v.get("mtime") or "", v.get("cc") or 0
        v["mtime"], v["cc"] = mtime, cc
        dirty = False
        if cc != old_cc:
            dirty = self.on_comment_change(gid, tid, key, topic, old_cc, cc) or dirty
        # 没编辑过的话题 modify_time 是空的, 第一次被编辑才出现 → 空字符串也当基线用
        if mtime != old_m and (mtime or old_m):
            dirty = self.on_edited(gid, tid, key, topic, old_cc, cc) or dirty
        return dirty

    def on_comment_change(self, gid, tid, key, topic, old_cc, new_cc):
        """评论数变了。涨还是跌, 从数字本身就知道, 不花请求;
        要说出「谁说了什么」才去拉评论 —— 所以提醒档位决定了花多少请求。"""
        title = topic_title(topic) or self.state[key].get("title") or tid
        gname = self.gnames.get(gid, gid)
        grew = new_cc > old_cc
        note = "%d → %d 条评论" % (old_cc, new_cc)
        self.add_event("comment", gid, tid, title, note)      # 不管哪一档, 本地都留一笔
        mode = self.opt(gid, "comment_alert")
        if mode == "off":
            log("    [·] 评论数变化: %s (%s), 评论提醒已全关" % (tid, note))
            return True
        want = (mode == "both" or (mode == "added" and grew)
                or (mode == "removed" and not grew))
        if not want and not grew:
            # 只有「只提醒新增」碰上评论变少会走到这: 不在意的方向, 一个请求都不花
            log("    [·] 评论变少: %s (%s), 这一档只报新增, 不处理" % (tid, note))
            return True
        if not self.archive.load(gid, tid):
            log("    [·] 评论数变化: %s (%s), 本地没这条的存档, 没有基线可比" % (tid, note))
            return True
        added, gone, complete, fetched = self.comment_delta(gid, tid, old_cc, new_cc)
        if not want:
            # 「只提醒减少」碰上新增: 新评论照样补进存档, 只是不出声
            log("    [·] 评论有变动: %s (%s) 新增 %d 条, 已入档, 这一档不提醒" % (
                tid, note, len(added)))
            return True
        log("    [!] 评论有变动: %s (%s) 新增 %d 条、少了 %d 条%s" % (
            tid, note, len(added), len(gone), (", 比对不完整" if not complete else "")))
        if not added and not gone and complete:
            log("    [·] 数字动了但评论一条没变 (比如子回复被删), 不发消息")
            return True
        # 文本告警 (企业微信 / 钉钉 / Bark / webhook 用)
        lines = [note]
        for c, parent in added[:5]:
            who = ((c.get("owner") or {}).get("name")) or "?"
            if parent is not None:
                who += " 回复 " + (((c.get("repliee") or {}).get("name")) or "?")
            body = " ".join((c.get("text") or "").split())[:60] or "(图片)"
            lines.append("· %s: %s" % (who, body))
        for c, parent in gone[:5]:
            who = ((c.get("owner") or {}).get("name")) or "?"
            body = " ".join((c.get("text") or "").split())[:60] or "(图片)"
            lines.append("· 不见了 —— %s: %s" % (who, body))
        if not complete:
            lines.append("（评论上限挡着，只比了前面那部分；调成「全部」就齐了）")
        pushed = bool(self.bot) and self.bot.push_comment_change(
            gid, tid, title, gname, new_cc, added, gone, complete)
        self.notify("💬 话题评论有变动", "星球: %s\n标题: %s\n%s\n%s" % (
            gname, title[:60], "\n".join(lines), self.topic_link(gid, tid)),
            skip_telegram=pushed)
        return True

    def comment_delta(self, gid, tid, old_cc, new_cc):
        """评论数变了 → 拉一次评论跟本地基线比。返回 (新增, 消失, 是否比全了, 抓到的评论)。
        涨了只要新的那几条 (begin_time 游标), 拿到的是尾巴, 不能拿来判断谁消失了;
        掉了才整段拉一遍, 那时才有资格说少了谁。"""
        raw = self.archive.load(gid, tid)
        stored = Archive.comments_of(raw)
        limit = int(self.opt(gid, "max_comments") or 0)
        if not raw:
            # 本地没存档 (清理过或从未成功备份), 没有基线可比, 别把整串评论说成「新来的」
            return [], [], False, self.cli.comments(tid, limit=limit)
        complete = True
        if new_cc > old_cc and stored:
            begin = max((c.get("create_time") or "") for c in stored) or None
            fetched = self.cli.comments(tid, limit=0, begin=begin)
            added, _tail = Archive.diff_comments(stored, fetched)
            gone = []                             # 只拉到尾巴, 老评论不在里面很正常
            if not fetched:                       # 游标什么都没给回来: 整段拉一次兜底
                fetched = self.cli.comments(tid, limit=limit)
                added, gone = Archive.diff_comments(stored, fetched)
                complete = (not limit) or Archive.comment_total(fetched) >= new_cc
        else:
            fetched = self.cli.comments(tid, limit=limit)
            added, gone = Archive.diff_comments(stored, fetched)
            # 只取了前 limit 条就没法断言「谁不见了」, 也别谎报比全了
            complete = (not limit) or Archive.comment_total(fetched) >= new_cc
        if not complete:
            gone = []
        if fetched:
            self.archive.append_comments(gid, tid, fetched, cc=new_cc)
        return added, gone, complete, fetched

    def on_edited(self, gid, tid, key, topic, old_cc, new_cc):
        """话题被编辑: 存一份修订快照, 算好逐行差异再告警。"""
        gname = self.gnames.get(gid, gid)
        # 列表里的正文可能被服务端截断, 这里单独拉一次全量
        d = self.cli.req("/v2/topics/%s" % tid)
        snap = ((d.get("resp_data") or {}).get("topic") or {}) if d.get("succeeded") else {}
        snap = snap or topic
        n = self.archive.next_rev(gid, tid)
        prev = (self.archive.load_rev(gid, tid, n - 1) if n > 1
                else self.archive.load(gid, tid))
        limit = int(self.opt(gid, "max_comments") or 0)
        # 评论提醒全关 = 不再追评论; 修订快照只存话题本身, 不为它去拉评论
        chase = self.opt(gid, "comment_alert") != "off"
        comments = self.cli.comments(tid, limit=limit) if (chase and new_cc > 0) else []
        self.archive.save_rev(gid, tid, n, {
            "topic": snap, "comments": comments, "_group": gname,
            "_fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        self.state[key]["revs"] = n
        title = topic_title(snap) or topic_title(topic) or self.state[key].get("title") or tid
        diff = Archive.diff_topics(prev.get("topic") or {}, snap)
        change = "；".join("%s 新增 %d 行、删掉 %d 行" % (d0["label"], d0["added"], d0["removed"])
                           for d0 in diff) or "正文没变，可能只改了图片或附件"
        log("    [!] 话题有改动: %s (第 %d 次) %s" % (tid, n, change))
        self.add_event("edit", gid, tid, title, "第 %d 次改动·%s" % (n, change))
        pushed = bool(self.bot) and self.bot.push_edit_change(
            gid, tid, title, gname, n, diff)
        self.notify("✏️ 话题内容有变动", "星球: %s\n标题: %s\n%s\n快照: %s\n%s" % (
            gname, title[:80], change,
            self.archive.rev_path(gid, tid, n), self.topic_link(gid, tid)),
            skip_telegram=pushed)
        return True

    # ---------- 主循环 ----------

    def rebuild_index(self):
        """重建 index.md。老档案的 state 里没有标题, 这里顺手补一次 (之后就一直带着了),
        否则索引和机器人的清单里全是「(无标题)」。"""
        try:
            from zsxq_archive import build_index
            missing = [k for k, v in self.state.items() if not v.get("title")]
            filled = 0
            for key in missing:
                gid, tid = key.split("/", 1) if "/" in key else ("", "")
                if not gid:
                    continue
                t = topic_title((self.archive.load(gid, tid) or {}).get("topic") or {})
                if t:
                    self.state[key]["title"] = t[:80]
                    filled += 1
            if filled:
                log("[·] 给 %d 条老档案补上了标题" % filled)
                self.save_state()
            groups = [{"group_id": g, "name": n} for g, n in self.gnames.items()]
            build_index(self.out, groups, self.state)
        except Exception as e:
            log("[!] 索引重建失败: %s" % e)

    def run_once(self, gid=None):
        """跑一轮。给了 gid 就只跑那一个星球, 不给就所有星球各跑一轮 (--once 用这个)。"""
        if not self.auth_ok():
            log("[x] 凭证失效, 本轮跳过")
            return
        listed = self.ensure_groups()
        if not listed and not self.gnames:
            log("[x] 星球名单拉不到, 本轮跳过")
            return
        if listed:
            missing = [g for g in self.cfg["groups"] if str(g) not in self.gnames]
            if missing:
                log("[x] 这些星球不在账号里: %s" % ", ".join(missing))
                self.cfg["groups"] = [g for g in self.cfg["groups"] if str(g) not in missing]
        groups = [str(gid)] if gid else [str(g) for g in self.cfg["groups"]]
        before = len(self.state)
        for g in groups:
            try:
                self.poll_group(g)
            except Exception as e:
                log("[!] %s 抓取异常: %r" % (g, e))
        gained = len(self.state) - before
        if gained > 0:
            self.rebuild_index()
        self.prune_all()
        if gained > 0:                    # 没新内容就不刷日志, 间隔调到十几秒也不吵
            log("[·] %s 新增备份 %d, 状态共 %d 条" % (
                ",".join(groups), gained, len(self.state)))

    def prune_all(self):
        """限量清理: 十分钟做一次就够了, 没必要跟着抓取节奏跑。"""
        now = time.time()
        if now - self._last_prune < 600:
            return
        self._last_prune = now
        for gid, keep in (self.cfg.get("group_keep") or {}).items():
            if keep:
                self.prune_group(str(gid), int(keep))

    def run(self):
        self.ensure_groups()               # 先拉星球名字, 日志和机器人菜单都要用
        groups = [str(g) for g in self.cfg["groups"] if g]
        if groups:
            log("[+] 哨兵启动: 盯 %d 个星球, 各按自己的间隔抓" % len(groups))
            for g in groups:
                log("    " + self.describe(g))
        else:
            # 一个都没配也照常在线: 机器人里选个星球打开自动推送, 它就自己进盯守名单了
            log("[!] 还没有配置星球 —— 机器人照常在线, 用 /groups 挑一个开自动推送即可")
        next_run = {}
        while True:
            groups = [str(g) for g in self.cfg["groups"] if g]   # 机器人可能刚加了星球
            for g in groups:
                next_run.setdefault(g, 0.0)
            now = time.time()
            due = [g for g in groups if now >= next_run.get(g, 0) and not self.in_quiet(g)]
            if not due:
                time.sleep(2)                    # 到点才干活, 不是死循环硬刷
                continue
            for g in due:
                base = max(5.0, float(self.opt(g, "poll_interval") or 30))
                try:
                    self.run_once(g)
                except Exception as e:
                    log("[!] 轮询异常 (%s): %r" % (g, e))
                    base = 60                    # 出异常就多歇一会再试
                next_run[g] = time.time() + base * random.uniform(0.75, 1.25)

    def describe(self, gid, with_name=True):
        """把某个星球的实际节奏写成一行, 启动日志 / /status / 星球页都用它。"""
        def fmt(sec):
            if sec < 60:
                return "%.0f 秒" % sec
            if sec < 3600:
                return "%.0f 分钟" % (sec / 60.0)
            return "%.0f 小时" % (sec / 3600.0)
        ci = int(self.opt(gid, "check_interval") or 0)
        sw = int(self.opt(gid, "sweep_interval") or 0)
        qs, qe = (list(self.opt(gid, "quiet_hours") or [0, 0]) + [0, 0])[:2]
        body = "每 %s 抓一次 · %d 条/轮 · 删除复查 %s · 评论 %s · %s%s%s" % (
            fmt(float(self.opt(gid, "poll_interval") or 30)),
            int(self.opt(gid, "page_size") or 20),
            ("关闭" if ci <= 0 else "每 " + fmt(float(ci))),
            ("全部" if not int(self.opt(gid, "max_comments") or 0)
             else "%d 条" % int(self.opt(gid, "max_comments"))),
            ("下图" if self.opt(gid, "keep_media") else "不下图"),
            ("" if sw <= 0 else " · 抽查 每 " + fmt(float(sw))),
            ("" if qs == qe else " · %d-%d 点静默" % (qs, qe)))
        return ("%s: %s" % (self.gnames.get(str(gid), str(gid)), body)) if with_name else body


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
    if w.cfg["groups"]:
        w.notify("🟢 星球哨兵已启动",
                 "盯守 %d 个星球, 机器人菜单已就绪 (/menu)。\n抓取节奏可以逐个星球在设置里调。" %
                 len(w.cfg["groups"]))
    else:
        w.notify("🟢 星球哨兵已启动",
                 "还没有配置星球 —— 发 /groups 挑一个打开自动推送, 它就会自动进盯守名单。")
    return w.run()


if __name__ == "__main__":
    sys.exit(main())
