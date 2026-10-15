# zsxq-sentinel

知识星球私有备份工具:盯住你加入的星球,新内容出现后第一时间抓回本地;已备份的内容被删除、编辑或评论被清时,立即通过 Telegram 告警。内置 Telegram 菜单机器人,手机上就能完成查看、推送、扫码续期等全部操作。

设计目标只有一个:**内容在被删之前,已经在你手里。**

## 它做什么

- **即时备份** — 按人类浏览的节奏轮询(默认 30 秒,带随机抖动),每个新话题立即保存完整数据:正文、全部评论和楼中楼回复、图片、附件、音频,原始 JSON 先落盘再渲染 Markdown。
- **删除监测** — 定期复查近期话题,连续两轮确认消失才判定被删,删除瞬间推送告警,本地已留完整快照。
- **篡改监测** — 话题被编辑、评论数下降,分别告警并把修订版另存。
- **Telegram 机器人** — 见下。
- **凭证自助续期** — 凭证失效告警后,在 Telegram 里点一下,扫码即续,全程不用碰电脑。

## Telegram 菜单机器人

配好机器人 token 后自动随哨兵启动,长轮询工作,不需要公网端口。发 `/menu` 进入:

| 命令 | 功能 |
| --- | --- |
| `/menu` | 主菜单 |
| `/groups` | 列出账号加入的所有星球 |
| `/status` | 备份条数、删除记录、各星球开关状态 |
| `/login` | 生成微信登录二维码发给你,扫码后新凭证自动生效 |

进入某个星球后有三个开关:

- **📜 最新 10 条** — 立即拉取该星球最近 10 条话题正文推送到聊天窗口
- **🔔 自动推送** — 开启后,该星球每个新话题备份完成时,内容即刻推给你
- **✂ 保留条数** — 在不限量 / 50 / 100 / 200 / 500 / 1000 之间循环切换,只保留最近 N 条,超出自动清理(磁盘紧张时用)

所有开关状态持久化在配置文件里,重启不丢。

## 部署

需要:任意一台 VPS,Python 3.8+,`pip3 install pycryptodome`(扫码登录用)。

```bash
git clone https://github.com/Piracola/zsxq_backup /opt/zsxq && cd /opt/zsxq
cp watch.json.example watch.json
vi watch.json        # 填 groups(星球ID)、notify.telegram(bot token 和你的 chat_id)
python3 -X utf8 zsxq_watch.py --once     # 试跑一轮
```

常驻(推荐 systemd):

```ini
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
```

## 配置(watch.json)

| 字段 | 说明 |
| --- | --- |
| `groups` | 要盯的星球 ID 列表 |
| `poll_interval` | 新话题轮询间隔秒数,实际带 ±25% 抖动 |
| `check_interval` | 近期话题滚动复查节奏 |
| `recent_days` | 复查最近几天的话题 |
| `confirm_gap` | 删除判定的两轮确认最小间隔 |
| `bot_users` | 允许操作机器人的 Telegram 用户 ID;留空则允许所有私聊 |
| `notify.telegram` | bot token 与 chat_id,兼作机器人与告警通道 |
| `notify.*` | 也可选企业微信 / 钉钉 / Bark / 通用 webhook |

## 凭证

登录凭证是跟账号绑定的 access token,支持两种获取/续期方式:

1. **微信扫码(推荐)** — Telegram 里发 `/login`,或在 VPS 上跑 `python3 zsxq_login.py`,二维码发到聊天窗口,微信扫码确认后新凭证自动写入并热生效,服务无需重启。协议逆向自网页版登录流程。
2. **手机提取(备用)** — 已 root 的安卓机可用 `zsxq_token.py` 直接提取。

## 数据结构

```
backup/
├── state.json                  # 话题状态总账(含删除标记、复查时间)
├── index.md                    # 可读索引
├── raw/<gid>/<tid>.json        # 原始 JSON(含全部评论)——最高优先级数据
├── raw/<gid>/<tid>__rev_N.json # 被编辑话题的修订快照
├── deleted/<gid>/<tid>.json    # 确认被删除的话题:删除前完整快照
└── <星球名>_<gid>/              # Markdown + images/ + files/
```

原始 JSON 是本体,Markdown 只是视图。渲染器有 bug 也不会丢数据,`--render-only` 即可从 raw 重渲染。

## 命令行

```bash
python3 zsxq_watch.py                     # 常驻运行
python3 zsxq_watch.py --once              # 单轮(配 cron 亦可)
python3 zsxq_archive.py --list            # 列出账号所有星球
python3 zsxq_archive.py --group <gid>     # 全量归档指定星球
python3 zsxq_archive.py --render-only     # 不联网,从 raw 重渲染
python3 verify_archive.py <备份目录>       # 校验备份完整性
python3 zsxq_unpack.py <备份目录>          # 解包整理
```

## 频率与账号安全

所有请求都按人类节奏设计:轮询带随机抖动、单请求限速、删除判定要求两轮确认、无 WebSocket 长连接。机器人的「最新 10 条」是即点即拉,自动推送搭轮询顺风车,不产生额外请求模式。

## License

仅供个人备份自己已加入星球的内容使用。
