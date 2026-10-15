# 知识星球云端哨兵 (zsxq_watch)

监视指定知识星球的最新话题，发现新内容立即全量备份到本机；定期复查近期话题，检测**删除 / 编辑 / 评论被删**，并通过 Telegram 等渠道即时告警。

## 文件

| 文件 | 作用 |
| --- | --- |
| `zsxq_watch.py` | 哨兵主程序：轮询备份 + 删除监测 + IM 告警 |
| `zsxq_archive.py` | 归档核心库（Client / 渲染 / 下载），被 watch 调用 |
| `zsxq_token.py` | 从已 root 的安卓手机提取凭证（备用手段） |
| `zsxq_login.py` | 微信扫码登录：Telegram 收码 → 扫码 → 凭证自动更新（主力手段） |
| `watch.json.example` | 配置模板：星球 ID、轮询参数、Telegram 告警 |
| `zsxq_sync.py` | 工作站 → VPS 的 git 推送中转脚本 |
| `cred.json` | **不进 git**。含 access_token，用 scp 单独传 |

## VPS 部署（东京，一次性）

```bash
# 1. 推代码（本机执行，host 换成你的 VPS）
python zsxq_sync.py user@your-vps

# 2. VPS 上取出工作副本
ssh user@your-vps
git clone /srv/git/zsxq.git /opt/zsxq && cd /opt/zsxq

# 3. 传凭证（本机执行）
scp cred.json user@your-vps:/opt/zsxq/

# 4. 配置
cp watch.json.example watch.json && vi watch.json   # 填 Telegram token/chat_id

# 5. 试跑一轮
python3 -X utf8 zsxq_watch.py --once

# 6. 常驻
sudo tee /etc/systemd/system/zsxq-watch.service <<'EOF'
[Unit]
Description=zsxq sentinel
After=network-online.target

[Service]
WorkingDirectory=/opt/zsxq
ExecStart=/usr/bin/python3 -X utf8 zsxq_watch.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now zsxq-watch
journalctl -u zsxq-watch -f     # 看日志
```

## 代码更新（以后每次改完）

```bash
python zsxq_sync.py user@your-vps   # 本机提交并推送
ssh user@your-vps "cd /opt/zsxq && git pull && sudo systemctl restart zsxq-watch"
```

## 产出目录结构

```
zsxq_backup/
├── state.json              # 全部话题的状态（含删除标记、复查时间戳）
├── index.md                # 备份索引
├── raw/<gid>/<tid>.json    # 每个话题的原始 JSON（含全部评论，最高优先级数据）
├── raw/<gid>/<tid>__rev_N.json  # 内容被编辑后的修订快照
├── deleted/<gid>/<tid>.json     # 确认被删除的话题：含删除前的完整快照
└── <星球名>_<gid>/          # 可读 Markdown + images/ + files/
```

## 运行参数（watch.json）

- `poll_interval: 30` — 新话题轮询间隔，实际带 ±25% 随机抖动
- `check_interval: 600` — 滚动复查节奏，每轮检查「距上次检查最久」的一批近期话题
- `recent_days: 3` — 只复查最近 3 天归档的话题
- `confirm_gap: 150` — 删除判定需两轮确认的最小间隔，防瞬时误报
- `notify_new: false` — 新话题是否也推 Telegram

## 凭证过期怎么办

哨兵每轮自检凭证，失效时通过 Telegram 提醒。两种更新方式：

**方式一（推荐）：扫码登录，全程手机完成**

```bash
ssh user@your-vps "cd /opt/zsxq && pip3 install pycryptodome && python3 -X utf8 zsxq_login.py"
```

二维码发到 Telegram → 微信扫码确认 → 新凭证自动写入 cred.json，哨兵自动恢复。原理：逆向了网页版登录流程（微信开放平台扫码 → code → POST /v3/access_tokens，AES-CBC+RSA 加密协议），实测端到端可用。

**方式二（备用）：手机 root 提取**

```bash
python zsxq_token.py --json cred.json
scp cred.json user@your-vps:/opt/zsxq/    # 传上去即自动恢复，无需重启
```
