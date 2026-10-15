#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zsxq_archive.py — 知识星球「已购内容」完整归档工具 (v2)

针对"反复发现漏内容"的设计:
  * 每个话题的原始 JSON 全量落盘 (raw/<gid>/<topic_id>.json, 含话题+全部评论+全部子回复)。
    渲染只是 raw 的一个视图 —— 即使渲染器有 bug, 数据也不会丢。
  * --render-only: 从已存 raw JSON 重新渲染, 无需重新联网。修渲染逻辑只要几秒。
  * 评论用 begin_time 游标翻页 (page= 无效, 只会返回重复的首页)。
  * 子回复在评论的 replied_comments 里, 一条不漏(实测 54 条完整返回;
    replies_count 与实际条数的差额是已删除回复, 不是抓取缺失)。

已实测覆盖的内容维度:
  话题类型 talk/q&a/task/solution · 正文/提问/回答/作业/解答
  内联标记 web(链接)/text_bold(加粗)/hashtag(标签)/mention(@提及)
  图片(话题/提问/回答/评论/子回复全包含) · 文件与音频附件(talk.files 含 duration)
  一级评论 + 二级子回复(含 repliee 被回复人)
  点赞/赞赏/阅读数 · 精华 · 置顶 · 修改时间 · 标签

用法:
  python zsxq_archive.py --list
  python zsxq_archive.py --group 12345678901234
  python zsxq_archive.py --render-only          # 只重渲染, 不联网
"""
import argparse, hashlib, json, os, re, sys, time, uuid
import urllib.request, urllib.error, urllib.parse

API = "https://api.zsxq.com"
UA = "xiaomiquan/5.35.2 Android/Phone/14"
VER = {"v2": "2.88.0", "v3": "3.14.0"}     # 实测: 传错会静默返回空数据
COMMENT_PAGE = 30                          # 实测上限 30; 传 100 返回空


def log(m):
    print(m, flush=True)


def safe_name(s, maxlen=60):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", (s or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" .")
    return (s[:maxlen] or "untitled")


E_RE = re.compile(r"<e\s+([^>]+?)\s*/?>", re.S)


def render_text(s):
    """还原 <e type=... title=.../> 内联标记; 属性值都是 URL 编码。"""
    if not s:
        return ""
    def rep(m):
        a = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
        t = a.get("type")
        title = urllib.parse.unquote(a.get("title", ""))
        if t == "web":
            href = urllib.parse.unquote(a.get("href", ""))
            label = title or href
            if a.get("style") == "book":
                label = "📖 " + label
            return "[%s](%s)" % (label, href) if href else label
        if t == "text_bold":
            return "**%s**" % title
        return title
    return E_RE.sub(rep, s)


class Client:
    def __init__(self, token, aduid=None, sleep=1.0, timeout=30):
        self.token, self.aduid, self.sleep, self.timeout = token, aduid, sleep, timeout
        self._last, self.calls = 0.0, 0

    def _throttle(self):
        gap = time.time() - self._last
        if gap < self.sleep:
            time.sleep(self.sleep - gap)
        self._last = time.time()

    def req(self, path, retries=3):
        url = path if path.startswith("http") else API + path
        last = None
        for attempt in range(retries + 1):
            self._throttle()
            ts, rid = int(time.time()), str(uuid.uuid4())
            sig = hashlib.sha1(("%s %d %s" % (url, ts, rid)).encode()).hexdigest()
            ver = VER["v3"] if "/v3/" in url else VER["v2"]
            r = urllib.request.Request(url)
            for k, v in [("X-Aduid", self.aduid or rid), ("User-Agent", UA),
                         ("X-Version", ver), ("Authorization", self.token),
                         ("X-Request-Id", rid), ("X-Timestamp", str(ts)),
                         ("X-Signature", sig), ("X-Product", "zsxq"),
                         ("Cookie", "zsxq_access_token=" + self.token)]:
                r.add_header(k, v)
            try:
                self.calls += 1
                with urllib.request.urlopen(r, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8", "replace"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code in (429, 500, 502, 503) and attempt < retries:
                    time.sleep(2 * (attempt + 1)); last = "HTTP %s" % e.code; continue
                try:
                    return json.loads(body)
                except Exception:
                    return {"succeeded": False, "error": "HTTP %s" % e.code}
            except Exception as e:
                last = repr(e)
                if attempt < retries:
                    time.sleep(2 * (attempt + 1)); continue
                return {"succeeded": False, "error": last}
        return {"succeeded": False, "error": last}

    def whoami(self):
        return ((self.req("/v2/users/self").get("resp_data") or {}).get("user")) or {}

    def groups(self):
        out = []
        for g in ((self.req("/v3/groups?count=100").get("resp_data") or {}).get("groups")) or []:
            gid = g.get("group_id") or g.get("uid")   # 实测字段名是 uid
            if gid:
                g["group_id"] = str(gid)
            out.append(g)
        return out

    def topics(self, gid, scope="all", since=None):
        """话题翻页: end_time = 上一页末条 create_time, 由新到旧。"""
        end, seen = None, set()
        while True:
            q = {"scope": scope, "count": 20}
            if end:
                q["end_time"] = end
            d = self.req("/v2/groups/%s/topics?%s" % (gid, urllib.parse.urlencode(q)))
            if not d.get("succeeded"):
                log("    [!] 话题翻页中断: %s" % str(d.get("error"))[:100]); return
            batch = ((d.get("resp_data") or {}).get("topics")) or []
            if not batch:
                return
            for t in batch:
                tid = t.get("topic_id")
                if tid in seen:
                    continue
                seen.add(tid)
                ct = t.get("create_time") or ""
                if since and ct and ct[:10] < since:
                    return
                yield t
            if len(batch) < 20:
                return
            end = batch[-1].get("create_time")
            if not end:
                return

    def comments(self, tid):
        """评论翻页: 必须用 begin_time 游标 (page= 参数无效)。"""
        out, seen, cursor = [], set(), None
        while True:
            q = "count=%d&sort=asc" % COMMENT_PAGE
            if cursor:
                q += "&begin_time=" + urllib.parse.quote(cursor)
            d = self.req("/v2/topics/%s/comments?%s" % (tid, q))
            if not d.get("succeeded"):
                break
            cs = ((d.get("resp_data") or {}).get("comments")) or []
            fresh = [c for c in cs if c.get("comment_id") not in seen]
            for c in fresh:
                seen.add(c.get("comment_id"))
                out.append(c)
            if not cs or not fresh:
                break
            nxt = cs[-1].get("create_time")
            if not nxt or nxt == cursor:
                break
            cursor = nxt
        return out


def download(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return True
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        r = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(r, timeout=180) as resp, open(dest, "wb") as f:
            while True:
                b = resp.read(65536)
                if not b:
                    break
                f.write(b)
        return os.path.getsize(dest) > 0
    except Exception as e:
        log("      [!] 下载失败 %s: %s" % (os.path.basename(dest), str(e)[:60]))
        if os.path.exists(dest):
            os.remove(dest)
        return False


def collect_images(node, out=None, seen=None):
    """递归收集所有 images (话题/提问/回答/评论/子回复通吃)。"""
    if out is None:
        out, seen = [], set()
    if isinstance(node, dict):
        ims = node.get("images")
        if isinstance(ims, list):
            for im in ims:
                if not isinstance(im, dict):
                    continue
                u = (im.get("large") or im.get("original") or {}).get("url")
                if u and u not in seen:
                    seen.add(u)
                    base = os.path.basename(urllib.parse.urlparse(u).path) or "img"
                    if not re.search(r"\.(jpg|jpeg|png|gif|webp|bmp)$", base, re.I):
                        base += ".jpg"
                    out.append((u, base))
        for v in node.values():
            collect_images(v, out, seen)
    elif isinstance(node, list):
        for v in node:
            collect_images(v, out, seen)
    return out


def collect_files(topic):
    """talk.files: 普通附件与音频(duration>0)。"""
    out = []
    for f in ((topic.get("talk") or {}).get("files") or []):
        fid = f.get("file_id")
        if fid:
            out.append((fid, safe_name(f.get("name") or str(fid)),
                        f.get("size") or 0, f.get("duration") or 0))
    return out


def render_md(topic, comments, url2name, group_name):
    tid = topic.get("topic_id")
    title = render_text(topic.get("title") or "").strip() or "话题 %s" % tid
    ct = (topic.get("create_time") or "")[:19].replace("T", " ")

    def person(*nodes):
        for n in nodes:
            if n and (n.get("owner") or {}).get("name"):
                return n["owner"]["name"]
        return "?"

    q, a, talk = topic.get("question"), topic.get("answer"), topic.get("talk")
    author = person(a, talk, q, topic.get("task"), topic)
    L = ["# %s" % title, "",
         "- 星球: %s" % group_name,
         "- 时间: %s" % ct,
         "- 类型: %s" % (topic.get("type") or "talk"),
         "- 话题ID: %s" % tid,
         "- 作者: %s" % author,
         "- 点赞 %s · 评论 %s · 阅读 %s · 赞赏 %s" % (
             topic.get("likes_count", 0), topic.get("comments_count", 0),
             topic.get("readers_count", 0), topic.get("rewards_count", 0))]
    if q and (q.get("owner") or {}).get("name"):
        L.append("- 提问者: %s%s" % (q["owner"]["name"],
                                     "（匿名）" if q.get("anonymous") else ""))
    if q and (q.get("fee") or {}).get("amount"):
        L.append("- 提问费用: %s" % q["fee"]["amount"])
    if topic.get("digested"):
        L.append("- ★ 精华")
    if topic.get("sticky"):
        L.append("- 📌 置顶")
    if topic.get("modify_time"):
        L.append("- 修改于: %s" % (topic["modify_time"][:19].replace("T", " ")))
    if topic.get("annotation"):
        L.append("- 标签: %s" % render_text(topic["annotation"]).strip())
    L += ["", "---", ""]

    def block(label, node):
        if not node:
            return
        tx = render_text(node.get("text") or "").strip()
        if label:
            L.append("**【%s】**" % label)
        if tx:
            L.append(tx)
            L.append("")
        for im in (node.get("images") or []):
            u = (im.get("large") or im.get("original") or {}).get("url")
            if not u:
                continue
            nm = url2name.get(u)
            L.append("![图片](%s)" % ("images/" + urllib.parse.quote(nm) if nm else u))
            L.append("")

    block(None, talk)
    block("提问", q)
    block("回答", a)
    block("作业", topic.get("task"))
    block("解答", topic.get("solution"))

    total_replies = sum(len(c.get("replied_comments") or []) for c in comments)
    if comments:
        L += ["---", "",
              "## 评论 (%d 条评论, %d 条回复)" % (len(comments), total_replies), ""]

        def cimg(im, quote):
            u = (im.get("large") or im.get("original") or {}).get("url")
            if not u:
                return
            nm = url2name.get(u)
            L.append("%s![图片](%s)" % (quote,
                     "images/" + urllib.parse.quote(nm) if nm else u))
            L.append("")

        for i, c in enumerate(comments, 1):
            ow = (c.get("owner") or {}).get("name") or "?"
            ct2 = (c.get("create_time") or "")[:19].replace("T", " ")
            tags = []
            if c.get("sticky"):
                tags.append("置顶")
            if c.get("group_owner_liked"):
                tags.append("星主赞")
            if c.get("rewards_count"):
                tags.append("赞赏%d" % c["rewards_count"])
            L.append("**%d. %s** · %s · 👍%s%s" % (
                i, ow, ct2, c.get("likes_count", 0),
                (" · " + " · ".join(tags)) if tags else ""))
            body = render_text(c.get("text") or "")
            for ln in (body.split("\n") if body else [""]):
                L.append("> %s" % ln)
            L.append("")
            for im in (c.get("images") or []):
                cimg(im, "> ")
            for rep in (c.get("replied_comments") or []):
                rw = (rep.get("owner") or {}).get("name") or "?"
                to = (rep.get("repliee") or {}).get("name")
                rt = (rep.get("create_time") or "")[:19].replace("T", " ")
                head = "    └ **%s**" % rw + (" 回复 **%s**" % to if to else "")
                L.append("%s · %s · 👍%s" % (head, rt, rep.get("likes_count", 0)))
                rbody = render_text(rep.get("text") or "")
                for ln in (rbody.split("\n") if rbody else [""]):
                    L.append("      > %s" % ln)
                L.append("")
                for im in (rep.get("images") or []):
                    cimg(im, "      > ")
    return "\n".join(L)


def topic_title(topic):
    title = render_text(topic.get("title") or "").strip()
    if not title:
        for k in ("talk", "question", "answer", "task"):
            if (topic.get(k) or {}).get("text"):
                title = render_text(topic[k]["text"]).strip().split("\n")[0]
                break
    return title


def topic_filename(topic):
    """文件名 = 日期_标题_话题ID后缀。
    必须带 ID 后缀: 同一天同标题(尤其标题被截断后)会互相覆盖。"""
    tid = str(topic.get("topic_id"))
    ct = (topic.get("create_time") or "")[:19].replace(":", "").replace("T", "_")
    return "%s_%s_%s.md" % (ct[:10], safe_name(topic_title(topic), 36), tid[-8:])


def build_index(out, groups, state):
    with open(os.path.join(out, "index.md"), "w", encoding="utf-8") as f:
        f.write("# 知识星球备份索引\n\n")
        for g in groups:
            gid, gname = str(g.get("group_id")), g.get("name") or ""
            # 按时间排, 不是按话题 ID —— ID 是无序的, 按它排出来新内容会散在中间
            rows = sorted((v["time"], v["file"]) for k, v in state.items()
                          if k.startswith(gid + "/"))
            f.write("\n## %s (%s) — %d 条\n\n" % (gname, gid, len(rows)))
            for t, path in rows[::-1]:
                f.write("- [%s](%s)\n" % (t[:10], urllib.parse.quote(path)))


def main():
    ap = argparse.ArgumentParser(description="知识星球内容完整归档")
    ap.add_argument("--token"); ap.add_argument("--aduid")
    ap.add_argument("--cred", default="cred.json")
    ap.add_argument("--out", default="zsxq_backup")
    ap.add_argument("--group", action="append", default=[])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--since")
    ap.add_argument("--digests-only", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--no-files", action="store_true")
    ap.add_argument("--no-comments", action="store_true")
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--max-topics", type=int, default=0)
    ap.add_argument("--render-only", action="store_true", help="只从 raw JSON 重渲染, 不联网")
    ap.add_argument("--refresh", action="store_true", help="忽略 state 重新抓取")
    a = ap.parse_args()

    os.makedirs(a.out, exist_ok=True)
    state_path = os.path.join(a.out, "state.json")
    state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {}

    # 重渲染纯本地操作, 不需要凭证, 放在最前面
    if a.render_only:
        gmap = {}
        for k in state:
            gmap.setdefault(k.split("/")[0], 0)
        log("[+] 仅重渲染: %d 个星球 / %d 条记录" % (len(gmap), len(state)))
        done = 0
        for k, v in state.items():
            gid, tid = k.split("/", 1)
            rawp = os.path.join(a.out, "raw", gid, "%s.json" % tid)
            if not os.path.exists(rawp):
                continue
            raw = json.load(open(rawp, encoding="utf-8"))
            top = raw["topic"]
            gdir = os.path.join(a.out, safe_name(raw.get("_group") or gid, 50) + "_" + gid)
            md_path = os.path.join(gdir, topic_filename(top))
            os.makedirs(gdir, exist_ok=True)
            url2name = {u: n for u, n in (raw.get("_img_map") or [])}
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(render_md(top, raw.get("comments") or [],
                                  url2name, raw.get("_group", "")))
            v["file"] = os.path.relpath(md_path, a.out)
            done += 1
        json.dump(state, open(state_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        log("[+] 重渲染完成 %d 条" % done)
        return 0

    token, aduid = a.token, a.aduid
    if not token and os.path.exists(a.cred):
        c = json.load(open(a.cred, encoding="utf-8"))
        token, aduid = c.get("access_token"), c.get("aduid")
    if not token:
        log("[x] 缺少凭证。先运行: python zsxq_token.py --json cred.json"); return 1

    cli = Client(token, aduid, sleep=max(0.2, a.sleep))
    me = cli.whoami()
    if not me.get("name"):
        log("[x] 凭证无效或已过期"); return 1
    log("[+] 账号: %s (user_id=%s)" % (me["name"], me.get("user_id")))

    groups = cli.groups()
    if a.group:
        groups = [g for g in groups if str(g.get("group_id")) in a.group]
    if not groups:
        log("[!] 没有可归档的星球。"); return 1
    log("[+] 共 %d 个星球" % len(groups))
    for g in groups:
        log("    %-18s %s" % (g.get("group_id"), g.get("name")))
    if a.list:
        return 0

    scope = "digests" if a.digests_only else "all"
    grand = 0
    for gi, g in enumerate(groups, 1):
        gid, gname = str(g.get("group_id")), g.get("name") or ""
        gdir = os.path.join(a.out, safe_name(gname, 50) + "_" + gid)
        rawdir = os.path.join(a.out, "raw", gid)
        os.makedirs(gdir, exist_ok=True)
        os.makedirs(rawdir, exist_ok=True)
        log("\n[%d/%d] %s (%s)" % (gi, len(groups), gname, gid))

        n = 0
        for topic in cli.topics(gid, scope=scope, since=a.since):
            if a.max_topics and n >= a.max_topics:
                break
            n += 1
            tid = str(topic.get("topic_id"))
            if not a.refresh and ("%s/%s" % (gid, tid)) in state:
                continue

            comments = [] if a.no_comments else (
                cli.comments(tid) if (topic.get("comments_count") or 0) > 0 else [])

            # 先落原始数据 —— 之后无论渲染出什么问题都不必重抓
            rawpath = os.path.join(rawdir, "%s.json" % tid)
            with open(rawpath, "w", encoding="utf-8") as f:
                json.dump({"topic": topic, "comments": comments, "_group": gname,
                           "_fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False)

            url2name, file_names = [], []
            if not a.no_images:
                for url, base in collect_images(topic) + collect_images(comments):
                    if download(url, os.path.join(gdir, "images", base)):
                        url2name.append((url, base))
            if not a.no_files:
                for fid, nm, size, dur in collect_files(topic):
                    dd = cli.req("/v2/files/%s/download_url" % fid)
                    u = ((dd.get("resp_data") or {}).get("download_url"))
                    if u and download(u, os.path.join(gdir, "files", nm)):
                        file_names.append((u, nm, size, dur))

            with open(rawpath, "w", encoding="utf-8") as f:
                json.dump({"topic": topic, "comments": comments, "_group": gname,
                           "_img_map": url2name, "_files": file_names,
                           "_fetched_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                          f, ensure_ascii=False)

            fname = topic_filename(topic)
            with open(os.path.join(gdir, fname), "w", encoding="utf-8") as f:
                f.write(render_md(topic, comments, dict(url2name), gname))

            state["%s/%s" % (gid, tid)] = {
                "file": os.path.relpath(os.path.join(gdir, fname), a.out),
                "time": topic.get("create_time")}
            grand += 1
            if grand % 10 == 0:
                json.dump(state, open(state_path, "w", encoding="utf-8"),
                          ensure_ascii=False, indent=1)
                log("    ... 本次已归档 %d 条" % grand)
        log("    完成: 遍历 %d 条" % n)
        json.dump(state, open(state_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        build_index(a.out, groups, state)

    log("\n[+] 全部完成, 本次新增 %d 条 -> %s" % (grand, os.path.abspath(a.out)))
    log("    API 调用 %d 次" % cli.calls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
