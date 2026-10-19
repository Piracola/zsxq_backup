#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zsxq_watch.py — 知识星球云端哨兵

  * 按各星球自己的间隔看一眼最新话题, 发现新话题立即全量备份
    (raw JSON 先落盘 → 图片/附件 → Markdown), 内容被删之前已经拿回本地。
  * 复查搭同一趟车: 列表里每条已归档话题都当场比对修改时间与评论数, 不多发请求。
    只有"本轮窗口内本该出现、却没出现"的话题才单独确认一次:
      - 确认被删  → 快照写入 deleted/ 并发 IM 告警
      - 内容被编辑 → 修订版另存 raw/gid/tid__rev_N.json, 发告警
      - 评论数下降 → 发告警 (评论被删的前兆)
  * 抓取节奏逐个星球可调: 间隔、每轮页数、复查周期与窗口、评论上限、是否下图。
    Telegram 机器人的「抓取设置」里点按钮就能改, 不用手改 watch.json。
  * 凭证失效自动告警 (6 小时冷却), 凭证本身仍需手机端提取:
      python zsxq_token.py --json cred.json
  * IM 告警渠道: 企业微信机器人 / 钉钉机器人 / Telegram / Bark / 通用 webhook

用法:
  python zsxq_watch.py --cred cred.json --group 12345678901234 --out zsxq_backup
  python zsxq_watch.py --once          # 只跑一轮 (配合 cron 也行)
配置写在 watch.json (首次运行自动生成模板), 告警渠道全在里面。
"""
import argparse, json, os, random, sys, time, urllib.parse, urllib.request
from zsxq_archive import (Client, download, collect_images, collect_files,
                          render_md, topic_filename, safe_name, log)
from tg_bot import TgBot

API = "https://api.zsxq.com"


def _topic_gone(resp):
    """接口明确说"话题不存在/已删除"才算删除信号 (code 1007)。
    限流、5xx、网络错误、权限问题都不算 —— 这些留到下一轮复核, 别误报。"""
    try:
        if int(resp.get("code")) == 1007:
            return True
    except (TypeError, ValueError):
        pass
    return "不存在或已被删除" in str(resp.get("error") or "")


DEFAULT_CFG = {
    "groups": [],              # 要盯的星球 ID 列表; 命令行 --group 优先
    # ---- 抓取节奏: 下面是全局默认值, 单个星球可以在 group_opts 里单独覆盖 ----
    "poll_interval": 30,       # 抓新话题的间隔 (秒); 实际会加 ±25% 抖动, 避免请求过于规律
    "page_size": 20,           # 每轮拉多少条 (接口实测上限 30, 再多直接报"无效的count")
    "check_interval": 600,     # 窗口内缺席的话题, 同一话题两次确认的最小间隔 (秒); 0 = 不做确认
    "check_batch": 10,         # 每轮最多确认几条, 免得一次性扎堆
    "recent_days": 3,          # 确认范围上限: 只确认最近 N 天的话题
    "max_comments": 0,         # 每个话题最多抓多少条评论; 0 = 全部
    "keep_media": True,        # 是否下载图片与附件; False 只留 raw JSON 里的远程链接
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


def _ts(s):
    """create_time → 秒; 解析不了就当 0, 不要让它抛异常。"""
    try:
        return time.mktime(time.strptime((s or "")[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return 0.0


def load_cfg(path, cli_groups):
    cfg = dict(DEFAULT_CFG)
    if os.path.exists(path):
        try:
            user = json.load(open(path, encoding="utf-8"))
            cfg.update(user)
            cfg["notify"] = dict(DEFAULT_CFG["notify"])
            cfg["notify"].update(user.get("notify") or {})
            gone = [k for k in RETIRED if k in user]
            if gone:
                log("[·] 这些配置项已停用, 可以从 watch.json 删掉: %s (检测已并入每轮抓取)" %
                    ", ".join(gone))
        except Exception as e:
            log("[!] 配置文件解析失败, 使用默认配置: %s" % e)
    else:
        json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
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
        self.gnames = {}          # gid -> 名称
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
        json.dump(self.state, open(self.state_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)

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
        with open(rawpath, "w", encoding="utf-8") as f:
            json.dump({"topic": topic, "comments": comments, "_group": gname,
                       "_fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)

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

    def poll_group(self, gid):
        """抓一个星球的最新一页: 新话题备份, 老话题顺手比对, 最后确认窗口内缺席的。
        整轮只有这一个列表请求 —— 复查要的字段列表里本来就带着。"""
        gid = str(gid)
        gname = self.gnames.get(gid, gid)
        page = self.latest_page(gid)
        if not page:
            return
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
            if self.autopush_on(gid) and self.bot and self.bot.admins_or_any_chat():
                self.bot.push_topic(topic, gname)
            if self.opt(gid, "notify_new"):
                self.notify("📥 新话题已备份",
                            "星球: %s\n标题: %s\n%s" % (
                                gname, (topic.get("title") or "")[:60],
                                self.topic_link(gid, tid)))
        if dirty:                      # 基线变了才落盘, 别每轮都重写 state.json
            self.save_state()
        self.check_window(gid, page, seen)

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
            if _topic_gone(d):
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
        """拿一条话题的当前数据比对基线。列表返回的字段与详情接口完全一致,
        所以这一步不花任何请求。只关心修改时间与评论数两项。"""
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
        changed = []
        # 没编辑过的话题 modify_time 是空的, 第一次被编辑才出现 → 空字符串也当基线用
        if mtime != old_m and (mtime or old_m):
            changed.append("内容被编辑 (%s → %s)" % (old_m[:19] or "无", mtime[:19]))
        if cc < old_cc:
            changed.append("评论数下降 (%d → %d)" % (old_cc, cc))
        if not changed:
            return False
        # 修订快照另存, 原始 raw 不动。列表里的正文可能被服务端截断, 这里单独拉一次全量
        gname = self.gnames.get(gid, gid)
        d = self.cli.req("/v2/topics/%s" % tid)
        snap = ((d.get("resp_data") or {}).get("topic") or {}) if d.get("succeeded") else {}
        snap = snap or topic
        n = 1
        while os.path.exists(os.path.join(self.out, "raw", gid, "%s__rev_%d.json" % (tid, n))):
            n += 1
        revp = os.path.join(self.out, "raw", gid, "%s__rev_%d.json" % (tid, n))
        limit = int(self.opt(gid, "max_comments") or 0)
        comments = self.cli.comments(tid, limit=limit) if cc > 0 else []
        with open(revp, "w", encoding="utf-8") as f:
            json.dump({"topic": snap, "comments": comments, "_group": gname,
                       "_fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False)
        log("    [!] 话题有变动: %s (%s)" % (tid, "; ".join(changed)))
        self.notify("✏️ 话题内容有变动",
                    "星球: %s\n标题: %s\n%s\n快照: %s" % (
                        gname, (snap.get("title") or topic.get("title") or "")[:80],
                        "; ".join(changed), revp))
        return True

    # ---------- 主循环 ----------

    def rebuild_index(self):
        try:
            from zsxq_archive import build_index
            groups = [{"group_id": g, "name": n} for g, n in self.gnames.items()]
            build_index(self.out, groups, self.state)
        except Exception as e:
            log("[!] 索引重建失败: %s" % e)

    def run_once(self, gid=None):
        """跑一轮。给了 gid 就只跑那一个星球, 不给就所有星球各跑一轮 (--once 用这个)。"""
        if not self.auth_ok():
            log("[x] 凭证失效, 本轮跳过")
            return
        if not self.gnames:
            self.refresh_groups()
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
        groups = [str(g) for g in self.cfg["groups"] if g]
        if not groups:
            log("[x] 没有配置星球 (--group 或 watch.json 的 groups)")
            return 1
        log("[+] 哨兵启动: 盯 %d 个星球, 各按自己的间隔抓" % len(groups))
        for g in groups:
            log("    " + self.describe(g))
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

    def describe(self, gid):
        """把某个星球的实际节奏写成一行, 启动日志和 /status 都用它。"""
        def fmt(sec):
            if sec < 60:
                return "%.0fs" % sec
            if sec < 3600:
                return "%.0f分" % (sec / 60.0)
            return "%.0f小时" % (sec / 3600.0)
        ci = int(self.opt(gid, "check_interval") or 0)
        return "%s: 每 %s 抓一次 · %d 条/轮 · 复查 %s · 评论 %s · %s" % (
            self.gnames.get(gid, gid), fmt(float(self.opt(gid, "poll_interval") or 30)),
            int(self.opt(gid, "page_size") or 20),
            ("关闭" if ci <= 0 else "每" + fmt(float(ci))),
            ("全部" if not int(self.opt(gid, "max_comments") or 0)
             else "%d 条" % int(self.opt(gid, "max_comments"))),
            ("下图" if self.opt(gid, "keep_media") else "不下图"))


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
             "盯守 %d 个星球, 机器人菜单已就绪 (/menu)。\n抓取节奏可以逐个星球在设置里调。" %
             len(w.cfg["groups"]))
    return w.run()


if __name__ == "__main__":
    sys.exit(main())
