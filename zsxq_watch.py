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
import argparse, json, os, random, sys, time, urllib.parse, urllib.request
from zsxq_archive import (Client, download, collect_images, collect_files,
                          render_md, topic_filename, safe_name, log)
from tg_bot import TgBot, _fmt_topic_brief

API = "https://api.zsxq.com"

DEFAULT_CFG = {
    "groups": [],              # 要盯的星球 ID 列表; 命令行 --group 优先
    "poll_interval": 30,       # 新话题轮询间隔 (秒); 实际会加 ±25% 抖动, 避免请求过于规律
    "check_interval": 600,     # 单条话题的复查周期(秒): 距上次检查不足这么久就跳过
    "check_batch": 10,         # 每轮最多复查几条, 把复查摊开, 免得单轮请求扎堆
    "verify_gap": 5,           # 复查拉取失败后隔几秒重试; 两次都失败才判定被删除
    "recent_days": 3,          # 只复查最近 N 天归档的话题 (仅 check_archived 为真时生效)
    "check_archived": True,    # 是否复查已入库话题(删除/篡改监测); False = 只抓最新, 下过的永不再读
    "quiet_hours": [0, 0],     # 夜间静默 [起, 止] 本地小时, 支持跨零点 [22,6]; 起=止 表示不静默
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
                if self.autopush_on(gid) and self.bot.admins_or_any_chat():
                    self.bot.push_text(_fmt_topic_brief(topic, gname))
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
        # check_archived=false: 已入库的话题一律不再复查(省掉全部复查流量)。
        if not self.cfg.get("check_archived", True):
            return
        now = time.time()
        gap = self.cfg.get("confirm_gap", 150)
        interval = max(60, int(self.cfg.get("check_interval", 600)))  # 单条话题的复查周期
        batch = max(1, int(self.cfg.get("check_batch", 10)))          # 每轮最多查几条
        # 只复查"窗口内 且 已到复查周期"的话题; 先查距上次检查最久的
        keys = [k for k in self.recent_keys(self.cfg.get("recent_days", 7))
                if now - self.state[k].get("last_check", 0) >= interval]
        if not keys:
            return
        keys.sort(key=lambda k: self.state[k].get("last_check", 0))
        for key in keys[:batch]:
            gid, tid = key.split("/", 1)
            self.state[key]["last_check"] = time.time()
            d = self.cli.req("/v2/topics/%s" % tid)
            if not d.get("succeeded"):
                # 拉取异常先别定性: 隔几秒再拉一次, 两次都拿不到才算真出问题
                time.sleep(max(0.0, float(self.cfg.get("verify_gap", 5))))
                d2 = self.cli.req("/v2/topics/%s" % tid)
                if d2.get("succeeded"):
                    d = d2
                else:
                    self.on_missing(gid, tid, key, d2, gap, force_confirm=True)
                    continue
            self.on_detail(gid, tid, key, ((d.get("resp_data") or {}).get("topic") or {}))
        self.save_state()

    def on_missing(self, gid, tid, key, resp, gap, force_confirm=False):
        v = self.state[key]
        now = time.time()
        seen = v.get("gone_seen") or 0
        if not force_confirm:
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
        qs, qe = (list(self.cfg.get("quiet_hours") or [0, 0]) + [0, 0])[:2]
        if qs != qe:
            log("[·] 夜间静默 %d:00-%d:00 不轮询 (机器人线程仍在线)" % (qs, qe))
        while True:
            # 夜间静默: 只停轮询, 机器人照常在线 —— 夜里还能用 /menu, 只是不抓新内容
            if qs != qe:
                h = time.localtime().tm_hour
                if (qs <= h < qe) if qs < qe else (h >= qs or h < qe):
                    time.sleep(60)
                    continue
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
