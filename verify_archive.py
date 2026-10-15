#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_archive.py — 校验归档是否完整(查漏)

做三件事:
  1. 重新遍历线上话题列表, 与 state.json / raw 目录比对, 找出缺失的话题
  2. 检查每条 raw JSON 里 comments_count 与实际评论+回复条数的差异(列出可疑项)
  3. 统计图片/文件引用的本地化情况

用法:
  python verify_archive.py --cred cred.json --out zsxq_backup --group 12345678901234
"""
import argparse, json, os, sys, glob, importlib.util, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("za", os.path.join(HERE, "zsxq_archive.py"))
za = importlib.util.module_from_spec(spec); spec.loader.exec_module(za)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cred", default="cred.json")
    ap.add_argument("--out", default="zsxq_backup")
    ap.add_argument("--group", required=True)
    ap.add_argument("--sleep", type=float, default=0.5)
    a = ap.parse_args()

    c = json.load(open(a.cred, encoding="utf-8"))
    cli = za.Client(c["access_token"], c.get("aduid"), sleep=a.sleep)
    gid = a.group

    # 1) 线上话题集合
    print("[1/3] 遍历线上话题列表 ...")
    online = {}
    for t in cli.topics(gid, scope="all"):
        online[str(t.get("topic_id"))] = t
    print("      线上话题数: %d (API 调用 %d)" % (len(online), cli.calls))

    state_p = os.path.join(a.out, "state.json")
    state = json.load(open(state_p, encoding="utf-8")) if os.path.exists(state_p) else {}
    have = {k.split("/", 1)[1] for k in state if k.startswith(gid + "/")}
    print("      归档记录数: %d" % len(have))

    missing = sorted(set(online) - have)
    extra = sorted(have - set(online))
    print()
    if missing:
        print("  [x] 缺失 %d 条:" % len(missing))
        for m in missing[:20]:
            print("      %s  %s" % (m, (online[m].get("create_time") or "")[:19]))
    else:
        print("  [OK] 无缺失话题")
    if extra:
        print("  [!] 归档中有 %d 条线上已不存在(可能被删除): %s" % (len(extra), extra[:5]))

    # 2) raw 完整性
    print()
    print("[2/3] 检查 raw JSON 的评论完整性 ...")
    raws = glob.glob(os.path.join(a.out, "raw", gid, "*.json"))
    print("      raw 文件数: %d" % len(raws))
    no_raw = [t for t in have if not os.path.exists(
        os.path.join(a.out, "raw", gid, "%s.json" % t))]
    if no_raw:
        print("  [x] 有 %d 条归档记录缺 raw JSON" % len(no_raw))
    else:
        print("  [OK] 所有归档记录都有 raw JSON")

    suspicious = []
    tot_c = tot_r = 0
    for p in raws:
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception as e:
            suspicious.append((os.path.basename(p), "JSON 解析失败 %s" % e)); continue
        t = d.get("topic") or {}
        cs = d.get("comments") or []
        tot_c += len(cs)
        tot_r += sum(len(x.get("replied_comments") or []) for x in cs)
        cnt = t.get("comments_count") or 0
        got = len(cs) + sum(len(x.get("replied_comments") or []) for x in cs)
        # 差额通常是已删除评论(replies_count 不递减), 差额过大才可疑
        if cnt - got > 3:
            suspicious.append((str(t.get("topic_id")), "comments_count=%d 实得=%d" % (cnt, got)))
    print("      评论合计 %d 条, 子回复合计 %d 条" % (tot_c, tot_r))
    if suspicious:
        print("  [!] %d 条差额偏大(可能抓取不全, 也可能是已删除内容):" % len(suspicious))
        for tid, why in suspicious[:15]:
            print("      %s  %s" % (tid, why))
    else:
        print("  [OK] 评论/回复条数与 comments_count 基本一致")

    # 3) 资源本地化
    print()
    print("[3/3] 检查资源本地化 ...")
    img_ref = 0
    img_remote = 0
    for p in glob.glob(os.path.join(a.out, "*_%s" % gid, "*.md")):
        s = open(p, encoding="utf-8").read()
        img_ref += s.count("images/")
        img_remote += s.count("images.zsxq.com")
    print("      本地图片引用 %d 处; 仍指向远端 %d 处" % (img_ref, img_remote))
    n_img = len(glob.glob(os.path.join(a.out, "*_%s" % gid, "images", "*")))
    n_file = len(glob.glob(os.path.join(a.out, "*_%s" % gid, "files", "*")))
    print("      本地图片文件 %d 个; 附件 %d 个" % (n_img, n_file))
    print()
    print("[+] 校验结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
