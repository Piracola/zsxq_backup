#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tg_update.py — 机器人的「看看有没有新版 / 拉下来 / 重启」

只管三件事: 问 git 现在跑的是哪一版、远端有没有更新的、把代码拉到最新。
重启不在这里做 —— 由主进程在轮次之间把自己整个换掉 (os.execv), 免得在别人正写
state、机器人正发消息的时候把进程掐掉。

所有操作都跑在代码目录里 (`__file__` 那一层), 不猜路径。
只 fetch 不动工作区; 真要拉的时候也只走快进, 有本地改动就先停下来说清楚。
"""
import os
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


def _git(*args, timeout=30):
    """跑一条 git 命令。返回 (成功?, 输出, 错误说明)。"""
    try:
        p = subprocess.run(("git",) + args, cwd=HERE, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
    except FileNotFoundError:
        return False, "", "这台机器上没找到 git"
    except subprocess.TimeoutExpired:
        return False, "", "git 超时了（%d 秒），多半是网络不通" % timeout
    except Exception as e:
        return False, "", str(e)[:150]
    if p.returncode != 0:
        msg = (p.stderr or "").strip() or ((p.stdout or "").strip())
        return False, (p.stdout or "").strip(), (msg or "git 返回 %d" % p.returncode)[:300]
    return True, (p.stdout or "").strip(), ""


def is_repo():
    ok, out, _ = _git("rev-parse", "--is-inside-work-tree")
    return ok and out == "true"


def branch():
    ok, out, _ = _git("rev-parse", "--abbrev-ref", "HEAD")
    # 分离头指针 (直接 checkout 到某个提交) 时给空串, 让调用方退回默认分支
    return out if (ok and out and out != "HEAD") else ""


def current():
    """现在跑的是哪一版。读不到就给空 dict, 页面照常能开。"""
    ok, out, _ = _git("log", "-1", "--format=%h%x09%ci%x09%s")
    if not ok or not out:
        return {}
    parts = (out.split("\t") + ["", "", ""])[:3]
    return {"hash": parts[0], "when": parts[1][:16], "subject": parts[2][:70],
            "branch": branch() or "?"}


def dirty():
    """被改过的已跟踪文件。数据目录里的 raw/、state.json、watch.json 都是 gitignore 的,
    不算在内 —— 否则一更新就永远「仓库不干净」。"""
    ok, out, _ = _git("status", "--porcelain", "--untracked-files=no")
    return out.splitlines() if ok else []


def check(branch_name=None):
    """问远端有没有比本地新的提交。只 fetch, 不动工作区, 可以随便点。"""
    br = branch_name or branch() or "master"
    ok, _, err = _git("fetch", "--quiet", "origin", br, timeout=60)
    if not ok:
        return {"ok": False, "error": err, "behind": 0, "commits": [], "branch": br}
    ok, out, err = _git("log", "--format=%h%x09%s", "HEAD..origin/%s" % br)
    if not ok:
        return {"ok": False, "error": err, "behind": 0, "commits": [], "branch": br}
    rows = [ln.split("\t", 1) for ln in out.splitlines() if ln.strip()]
    return {"ok": True, "error": "", "behind": len(rows), "branch": br,
            "commits": [(r[0], (r[1] if len(r) > 1 else "")[:70]) for r in rows]}


def _compile_check(paths):
    """拉下来的 .py 连语法都过不了的话, 别拿它去重启 —— 那会把服务停在半路,
    而没有守护进程的时候, 停了就真没人把它拉起来了。"""
    bad = []
    for p in paths or []:
        if not str(p).endswith(".py"):
            continue
        full = p if os.path.isabs(str(p)) else os.path.join(HERE, str(p))
        if not os.path.exists(full):
            continue
        try:
            with open(full, "rb") as f:
                compile(f.read(), full, "exec")
        except Exception as e:
            bad.append("%s: %s" % (os.path.basename(full), str(e)[:120]))
    return bad


def pull():
    """快进到远端最新。有本地改动就停, 不用 --hard 覆盖任何东西。"""
    bad = dirty()
    if bad:
        return {"ok": False, "error": "代码目录里有没提交的改动，先别自动更新：\n" + "\n".join(bad[:5])}
    ok, before, _ = _git("rev-parse", "HEAD")
    if not ok or not before:
        return {"ok": False, "error": "读不到当前版本（这不是 git 仓库？）"}
    br = branch() or "master"
    ok, _, err = _git("fetch", "--quiet", "origin", br, timeout=60)
    if not ok:
        return {"ok": False, "error": err}
    ok, _, err = _git("merge", "--ff-only", "origin/%s" % br, timeout=90)
    if not ok:
        return {"ok": False, "error": err}
    ok, after, _ = _git("rev-parse", "HEAD")
    log, files = [], []
    if ok and after:
        ok2, out, _ = _git("log", "--format=%h %s", "%s..%s" % (before, after))
        log = out.splitlines()[:20] if ok2 else []
        ok3, out3, _ = _git("diff", "--name-only", before, after)
        files = out3.splitlines() if ok3 else []
    bad = _compile_check(files)
    if bad:
        _git("reset", "--hard", before)          # 树是干净的, 退回去不丢任何东西
        return {"ok": False, "from": before[:7], "error":
                "拉下来的代码连编译都过不了，已经退回原来的版本（没有重启，服务还是好的）：\n"
                + "\n".join(bad[:3])}
    return {"ok": True, "error": "", "from": before[:7], "to": (after or before)[:7],
            "log": log, "files": files, "branch": br}


def rollback(rev):
    """回到之前那一版。只动已跟踪文件, 工作区不干净就不动。"""
    if not rev:
        return {"ok": False, "error": "不知道要回到哪一版"}
    bad = dirty()
    if bad:
        return {"ok": False, "error": "代码目录里有没提交的改动，先别回滚"}
    ok, before, _ = _git("rev-parse", "HEAD")
    ok, _, err = _git("reset", "--hard", str(rev), timeout=60)
    if not ok:
        return {"ok": False, "error": err}
    ok, after, _ = _git("rev-parse", "HEAD")
    return {"ok": True, "error": "", "from": (before or "")[:7], "to": (after or "")[:7]}
