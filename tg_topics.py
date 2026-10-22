#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tg_topics.py — 把本地备份读成机器人要展示的东西

机器人里「翻备份 / 看一条话题 / 看评论 / 看改动对比」这些页面, 数据都从这里来。
这里不碰 telegram、自己不发请求, 所以能单独测; 哨兵比对评论和改动时用的也是
同一套函数, 两边看到的东西不会打架。

目录约定跟 zsxq_watch / zsxq_archive 一致:
  raw/<gid>/<tid>.json           话题 + 全部评论 (本体)
  raw/<gid>/<tid>__rev_N.json    第 N 次被改动时留下的快照
  deleted/<gid>/<tid>.json       确认被删时的完整快照
"""
import difflib
import json
import os
import time

from zsxq_archive import render_text, save_json, topic_blocks, topic_title

LIST_PAGE = 6        # 话题清单一页几条
COMMENT_PAGE = 5     # 评论一页几条
REV_LINE_MAX = 120   # 对比里一行最多显示多少字
REV_TTL = 15         # 修订快照目录的缓存秒数


def _ids(top_level):
    """{comment_id: (评论, 父评论或 None)} —— 顶层评论和楼中楼回复都在里面。"""
    idx = {}
    for c in (top_level or []):
        if not isinstance(c, dict):
            continue
        cid = c.get("comment_id")
        if cid not in (None, "None"):
            idx[str(cid)] = (c, None)
        for r in (c.get("replied_comments") or []):
            if not isinstance(r, dict):
                continue
            rid = r.get("comment_id")
            if rid not in (None, "None"):
                idx[str(rid)] = (r, c)
    return idx


def _clip_line(s):
    s = (s or "").replace("\t", " ").strip()
    return s if len(s) <= REV_LINE_MAX else s[:REV_LINE_MAX - 1] + "…"


def _block_diff(label, a, b):
    """一个区块的逐行差异; 没变化返回 None。"""
    if a == b:
        return None
    sm = difflib.SequenceMatcher(None, a, b)
    lines, added, removed = [], 0, 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        for x in a[i1:i2]:
            lines.append(("-", _clip_line(x)))
            removed += 1
        for x in b[j1:j2]:
            lines.append(("+", _clip_line(x)))
            added += 1
    return {"label": label, "added": added, "removed": removed, "lines": lines} if lines else None


class Archive:
    """备份目录的视图。几乎全是读操作, 只有补评论时才写回 raw。"""

    def __init__(self, out):
        self.out = out
        self._titles = {}
        self._revs = {}

    # ---------- 路径与读写 ----------

    def raw_path(self, gid, tid):
        return os.path.join(self.out, "raw", str(gid), "%s.json" % tid)

    def rev_path(self, gid, tid, n):
        return os.path.join(self.out, "raw", str(gid), "%s__rev_%s.json" % (tid, n))

    def deleted_path(self, gid, tid):
        return os.path.join(self.out, "deleted", str(gid), "%s.json" % tid)

    @staticmethod
    def read(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def load(self, gid, tid):
        """一条话题的存档 {topic, comments, _group, _img_map, ...}; 没有就是 {}。"""
        return self.read(self.raw_path(gid, tid)) or {}

    def load_rev(self, gid, tid, n):
        return self.read(self.rev_path(gid, tid, n)) or {}

    def load_deleted(self, gid, tid):
        return self.read(self.deleted_path(gid, tid)) or {}

    def save(self, gid, tid, payload):
        save_json(self.raw_path(gid, tid), payload)

    def save_rev(self, gid, tid, n, payload):
        save_json(self.rev_path(gid, tid, n), payload)
        self._revs.pop(str(gid), None)

    def next_rev(self, gid, tid):
        n = 1
        while os.path.exists(self.rev_path(gid, tid, n)):
            n += 1
        return n

    def rev_numbers(self, gid, tid):
        """这条话题的所有修订快照编号, 从小到大。目录列表带缓存, 免得每页都扫一遍。"""
        gid = str(gid)
        stamp, table = self._revs.get(gid, (0.0, {}))
        if time.time() - stamp > REV_TTL:
            table = {}
            try:
                for nm in os.listdir(os.path.join(self.out, "raw", gid)):
                    if nm.endswith(".json") and "__rev_" in nm:
                        base, _, num = nm[:-5].partition("__rev_")
                        if num.isdigit():
                            table.setdefault(base, []).append(int(num))
            except OSError:
                pass
            self._revs[gid] = (time.time(), table)
        return sorted(table.get(str(tid), []))

    def rev_count(self, gid, tid, rec=None):
        n = (rec or {}).get("revs")
        return int(n) if n is not None else len(self.rev_numbers(gid, tid))

    # ---------- 清单与标题 ----------

    def title(self, gid, tid, rec=None):
        """话题标题: state 里记了就用, 没记就翻 raw (结果留在内存里)。"""
        tid = str(tid)
        if tid not in self._titles:
            t = ((rec or {}).get("title") or "").strip()
            if not t:
                t = (topic_title((self.load(gid, tid) or {}).get("topic") or {}) or "").strip()
            self._titles[tid] = t or "(无标题)"
        return self._titles[tid]

    def forget_title(self, tid):
        self._titles.pop(str(tid), None)

    def entries(self, state, gid=None):
        """备份清单, 按时间从新到旧。只读 state, 不碰文件。"""
        out = []
        for key, rec in (state or {}).items():
            if not isinstance(rec, dict) or "/" not in key:
                continue
            g, tid = key.split("/", 1)
            if gid is not None and g != str(gid):
                continue
            out.append({"gid": g, "tid": tid,
                        "time": rec.get("time") or "",
                        "cc": int(rec.get("cc") or 0),
                        "deleted": bool(rec.get("deleted")),
                        "revs": self.rev_count(g, tid, rec),
                        "rec": rec})
        out.sort(key=lambda e: (e["time"], e["tid"]), reverse=True)
        return out

    def search(self, state, word, limit=40):
        """按标题搜 (正文不搜, 搜正文太慢)。第一次搜要读一遍 raw, 之后走缓存。"""
        w = (word or "").strip().lower()
        if not w:
            return []
        hits = []
        for e in self.entries(state):
            t = self.title(e["gid"], e["tid"], e["rec"])
            if w in t.lower():
                e["title"] = t
                hits.append(e)
                if len(hits) >= limit:
                    break
        return hits

    # ---------- 评论 ----------

    @staticmethod
    def comments_of(raw):
        return (raw or {}).get("comments") or []

    @staticmethod
    def comment_total(comments):
        """评论条数按星球自己的算法: 主评论 + 楼中楼回复。
        接口里的 comments_count 就是这么算的 (拿真实存档核过 4745 条话题),
        所以凡是拿本地条数跟它比, 都得按这个口径, 不然有楼中楼的话题永远显得"不全"。"""
        return sum(1 + len(c.get("replied_comments") or [])
                   for c in (comments or []) if isinstance(c, dict))

    @staticmethod
    def reply_total(comments):
        return sum(len(c.get("replied_comments") or [])
                   for c in (comments or []) if isinstance(c, dict))

    @staticmethod
    def comment_index(raw):
        """{comment_id: (评论, 父评论或 None)}"""
        return _ids((raw or {}).get("comments") or [])

    @staticmethod
    def merge_comments(stored, fetched):
        """两批评论并起来: 按 comment_id 去重, 顶层按时间排, 回复挂回父评论下面。
        只增不减 —— 服务器上删掉的评论, 本地留着的还留着。"""
        tops, kids = {}, {}

        def take(top_list):
            for c in (top_list or []):
                if not isinstance(c, dict):
                    continue
                cid = c.get("comment_id")
                if cid in (None, "None"):
                    continue
                cid = str(cid)
                old = tops.get(cid)
                if old is None or (c.get("create_time") or "") >= (old.get("create_time") or ""):
                    tops[cid] = c
                bucket = kids.setdefault(cid, {})
                for r in (c.get("replied_comments") or []):
                    if isinstance(r, dict) and r.get("comment_id") not in (None, "None"):
                        bucket[str(r.get("comment_id"))] = r

        take(stored)
        take(fetched)
        out = []
        for cid in sorted(tops, key=lambda k: (tops[k].get("create_time") or "", k)):
            c = dict(tops[cid])
            reps = kids.get(cid) or {}
            if reps:
                c["replied_comments"] = sorted(
                    reps.values(), key=lambda r: (r.get("create_time") or ""))
            else:
                c.pop("replied_comments", None)
            out.append(c)
        return out

    @staticmethod
    def diff_comments(stored_top, fetched_top):
        """比对两批评论, 返回 (新增, 消失), 元素都是 (评论, 父评论或 None)。"""
        old, new = _ids(stored_top), _ids(fetched_top)
        return ([new[i] for i in new if i not in old],
                [old[i] for i in old if i not in new])

    def append_comments(self, gid, tid, fetched, cc=None):
        """把新抓到的评论并进 raw 写回。返回 (合并后的 raw, 新增列表)。
        cc 给了就把话题里的评论数一起改掉, 免得 raw 和 Markdown 上还写着旧数字。"""
        raw = self.load(gid, tid)
        if not raw:
            return {}, []
        old = _ids(raw.get("comments") or [])
        raw["comments"] = self.merge_comments(raw.get("comments"), fetched)
        raw["_comments_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if cc is not None and isinstance(raw.get("topic"), dict):
            raw["topic"]["comments_count"] = int(cc)
        self.save(gid, tid, raw)
        new = _ids(fetched)
        return raw, [new[i] for i in new if i not in old]

    # ---------- 改动对比 ----------

    @staticmethod
    def diff_topics(old, new):
        """比对两个版本的话题, 返回只含变化的区块:
        [{"label": "正文", "added": 2, "removed": 1, "lines": [("-", "..."), ("+", "...")]}]"""
        def text_lines(text):
            return [ln.strip() for ln in render_text(text or "").splitlines() if ln.strip()]

        def blocks(topic):
            return {lab or "正文": txt for lab, txt, _ in topic_blocks(topic or {})}

        out = []
        head = _block_diff("标题", text_lines((old or {}).get("title")),
                           text_lines((new or {}).get("title")))
        if head:
            out.append(head)
        a, b = blocks(old), blocks(new)
        for label in ("正文", "提问", "回答", "作业", "解答"):
            if label not in a and label not in b:
                continue
            d = _block_diff(label, text_lines(a.get(label)), text_lines(b.get(label)))
            if d:
                out.append(d)
        return out
