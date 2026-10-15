#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zsxq_sync.py — 从这台机器 (Windows 工作站) 把仓库推送到 VPS (东京)

用法:
  python zsxq_sync.py user@1.2.3.4        # 首次: 初始化远端 + 推送
  python zsxq_sync.py user@1.2.3.4        # 之后: 有更新就再推一次
依赖: 本机已装 git 和 ssh (Win11 自带), 且已配好到 VPS 的 SSH 免密(或愿意手输密码)。

仓库里没有 cred.json —— 凭证含 token, 不进 git, 用 scp 单独传:
  scp cred.json user@1.2.3.4:/opt/zsxq/
"""
import subprocess, sys, time

SRC = ["zsxq_archive.py", "zsxq_watch.py", "zsxq_token.py", "watch.json.example",
       "README.md", ".gitignore"]


def sh(cmd):
    p = subprocess.run(cmd, shell=True, capture_output=True)
    out = p.stdout.decode("utf-8", "replace")
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0:
        print(err or out)
        sys.exit(p.returncode)
    return out


def main():
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        print(__doc__)
        return 1
    host = sys.argv[1].strip()
    remote = "vps"

    print("[1/4] 检查仓库状态...")
    if not sh("git remote").split():
        print("      添加 remote: " + host + ":/srv/git/zsxq.git")
        sh("git remote add %s ssh://%s/srv/git/zsxq.git" % (remote, host))
    else:
        sh("git remote set-url %s ssh://%s/srv/git/zsxq.git" % (remote, host))

    print("[2/4] 提交本地改动...")
    sh("git add -A")
    diff = sh("git status --porcelain")
    if diff.strip():
        sh('git commit -m "sync: update %s"' % time.strftime("%Y-%m-%d %H:%M"))
    else:
        print("      没有新改动")

    print("[3/4] 在 VPS 上准备裸仓库...")
    sh("ssh %s \"mkdir -p /srv/git && git init --bare -q /srv/git/zsxq.git 2>/dev/null; true\"" % host)

    print("[4/4] 推送...")
    sh("git push -u %s master" % remote)

    print("\n完成。VPS 侧还需要手工做两件事 (只做一次):")
    print("  ssh %s" % host)
    print("  git clone /srv/git/zsxq.git /opt/zsxq")
    print("  scp cred.json 上去:  在本机执行  scp cred.json %s:/opt/zsxq/" % host)
    return 0


if __name__ == "__main__":
    sys.exit(main())
