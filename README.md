# zsxq-sentinel

知识星球私有备份工具:盯住你加入的星球,新内容出现后第一时间抓回本地;已备份的内容被删除、编辑或评论被清时,立即通过 Telegram 告警。内置 Telegram 菜单机器人,手机上就能完成查看、推送、扫码续期等全部操作。

设计目标只有一个:**内容在被删之前,已经在你手里。**

## 它做什么

- **即时备份** — 按人类浏览的节奏轮询(默认 30 秒,带随机抖动),每个新话题立即保存完整数据:正文、全部评论和楼中楼回复、图片、附件、音频,原始 JSON 先落盘再渲染 Markdown。
- **删除监测** — 按 `check_interval` 周期复查窗口(`recent_days`)内话题,连续两次拿到「话题不存在」的明确回应才判定被删;限流、5xx、网络异常留到下一轮复核,不会误报。判定瞬间推送告警,本地已留完整快照。
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

推送话题时会带图:提问、回答、正文里的图片按区块单独发出来(一组最多 10 张,超出的分开发),问答里提问方的附图不会再只剩一段文字。

所有开关状态持久化在配置文件里,重启不丢。

## 部署

需要:任意一台 VPS,Python 3.10+

```bash
git clone https://github.com/Piracola/zsxq_backup /opt/zsxq && cd /opt/zsxq
pip3 install -r requirements.txt          # python-telegram-bot + pycryptodome
cp watch.json.example watch.json
vi watch.json        # 填 groups(星球ID)、notify.telegram(bot token 和你的 chat_id)
python3 -X utf8 test_tg_bot.py           # 自检: 不联网, 验证机器人逻辑
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

## 代码结构

| 文件 | 作用 |
| --- | --- |
| `zsxq_watch.py` | 哨兵主程序与唯一入口:轮询备份、删除/篡改监测、IM 告警 |
| `tg_bot.py` | Telegram 菜单机器人(python-telegram-bot 22.x),随哨兵启动,无需单独运行 |
| `zsxq_archive.py` | 归档核心库(Client / 渲染 / 下载),被 watch 调用 |
| `zsxq_login.py` | 微信扫码登录,可独立运行,也被机器人 /login 调用 |
| `zsxq_token.py` | 已 root 安卓机提取凭证(备用) |
| `zsxq_sync.py` | 工作站 → VPS 的 git 推送中转 |
| `test_tg_bot.py` | 机器人离线自检(不联网),改完机器人跑一下 |
| `verify_archive.py` / `zsxq_unpack.py` | 备份校验 / 解包整理 |
| `requirements.txt` | 依赖清单 |

机器人跑在哨兵进程内的独立线程里,自带 asyncio 事件循环:哨兵是同步世界,通过跨线程投递把新话题推给机器人;所有拉取/清理类阻塞操作都丢到线程池,所以你在手机上点按钮时菜单不会被卡住。

依赖方向: `zsxq_watch` → `tg_bot` → `zsxq_login`;`zsxq_watch` 与 `tg_bot` → `zsxq_archive`。无反向依赖,配置与备份状态由 Sentinel 单点持有,机器人线程只读共享实例。

## 配置(watch.json)

| 字段 | 说明 |
| --- | --- |
| `groups` | 要盯的星球 ID 列表 |
| `poll_interval` | 新话题轮询间隔秒数,实际带 ±25% 抖动 |
| `check_archived` | 是否复查已入库话题(删除/篡改监测);`false` 则只抓最新,下过的不再读 |
| `check_interval` | 单条话题的复查周期秒数,距上次检查不足这么久就跳过 |
| `check_batch` | 每轮最多复查几条,把复查摊开 |
| `verify_gap` | 复查拉取失败后隔几秒重试;两次都失败时先排除凭证问题,只有接口明确说话题不存在才当场判定,其它异常留到下一轮 |
| `auth_cache_ttl` | 凭证自检结果复用秒数(默认 1800),省掉每轮一次 whoami;`0` 为每轮都查 |
| `recent_days` | 复查窗口:只复查最近几天话题,更早的不碰 |
| `confirm_gap` | 异常类失败跨轮复核的最小间隔(明确的「话题不存在」信号不受它影响) |
| `quiet_hours` | 夜间静默 `[起, 止]` 本地小时(支持跨零点如 `[22,6]`),静默期内不轮询,机器人仍在线 |
| `bot_users` | 允许操作机器人的 Telegram 用户 ID;留空则允许所有私聊 |
| `notify.telegram` | bot token 与 chat_id,兼作机器人与告警通道 |
| `notify.*` | 也可选企业微信 / 钉钉 / Bark / 通用 webhook |

请求量参考(单星球):每轮固定 1 次最新列表;凭证自检结果按 `auth_cache_ttl` 缓存(默认 30 分钟),不是每轮都问 whoami。复查按 `recent_days` 窗口内、到 `check_interval` 的话题分批进行。想更省,**第一优先是把 `poll_interval` 调大**(它决定轮次总数),其次才是复查窗口。

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
python3 test_tg_bot.py                    # 机器人离线自检(不联网)
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
