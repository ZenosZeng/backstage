# 工作区工具

这些是旁路工具，不依赖业务仓库的 Python 环境。watchdog 只读日志、结果 JSON
和进程/Kubernetes 状态，不启动、停止或修改训练、仿真、推理和评分。
Claude 飞书遥控是另一项独立服务，不与监控告警共用进程或凭据。

## 安装与启动

```bash
cd ~/code/.agents
pixi install --locked
pixi run watchdog-b1k
pixi run watchdog-train
pixi run tools-status
```

两个 watchdog 按需启动，不要求在同一台机器运行。默认后台化，SSH 断开仍运行。
同名服务只允许一个实例；配置改变需先停止旧实例。无需 sudo、tmux 或业务环境。

```bash
pixi run watchdog-b1k --status
pixi run watchdog-b1k --stop
pixi run watchdog-train --once
pixi run watchdog-b1k --foreground --no-notify
```

三个服务均支持 `--status`、`--stop`、`--once`、`--foreground` 和 `--config PATH`。
`--once` 是无通知、无常驻进程的配置与只读状态检查；遥控的 `--once` 不连接飞书或运行 Claude。
`--no-notify` 仅禁用 watchdog 消息，不是暂停遥控指令的开关。
PID 身份通过启动时间、命令、工作目录核验；不会用宽泛 `pkill` 停止其他服务。

## 本机配置

模板在 `tools/config/`；私有配置放 `.local/config/<服务名>.json`，不进 Git/S3。
没有配置时，默认 workspace 来自根目录 `config.json`，否则采用 `.agents` 的父目录。
模板中的 `~/code` 只是示例，可以改成其他工作区。相对路径以配置文件所在目录为基准。

| 服务 | 可选字段 |
|---|---|
| watchdog-b1k | `repo_root`、`log`、`eval_config` |
| watchdog-train | `repo_root`、`log_root`、`kubectl_args`、`experiments` |
| claude-feishu | `credentials_file`、`workdir`、`claude_cli`、`timeout_seconds`、`permission_profile` |

B1K 默认查找 `scripts/eval/{fleet,0srv16sim,2srv14sim,8srv8sim}/eval.log` 中最新日志。
自定义请求建议显式指定 `log` 和 `eval_config`；兼容 `B1K_EVAL_LOG` / `B1K_EVAL_CONFIG`，
但 JSON 显式字段优先。汇总只读当前 TOML 选中的 summary，保留 EMA/raw、namespace、horizon/steps 区别。

Pi 默认查找 workspace 的 `Pi_b1k`（存在时）或 `Pi`，读取其 `kjob_logs/`。
`kubectl_args` 可配置 context/namespace，例如 `["--context", "cluster", "-n", "training"]`。
实验优先通过 Kubernetes job 的提交脚本注解识别；历史串行启动器可在本机
`experiments` 填 `["显示名", "*日志匹配字段*"]`，不把实验清单写入通用代码。
保留训练 step/loss/ETA、离线评测状态与子日志活跃度、失败日志、停滞检测。

共用监控设置：`poll_seconds=60`；整点发送例行状态；默认 0-7 点和 13 点静默，告警不静默。
B1K 停滞提醒/严重阈值为 7200/10800 秒；Pi 为 900/3600 秒，可通过
`stall_warning_seconds` / `stall_critical_seconds` 调整。
Fleet 日志默认使用 900/3600 秒，告警每分钟检查。正式多机评测前应显式设置
`log` 为 fleet 的 `eval.log`、`eval_config` 为本次 TOML，重启 watchdog 后检查 `--status`。
七类卡片为任务开始、完成、故障、停滞、整点状态、监控启动、监控停止。
同内容成功发送后 600 秒去重；HTTP 200 仍检查业务 `code == 0`。

## 飞书凭据

监控从环境读 `FEISHU_WEBHOOK_KEY`（也支持 `FEISHU_WEBHOOK` 完整 URL），签名使用可选
`FEISHU_SECRET`。兼容从 `.zshrc` / `.bashrc` 读取对应赋值，但不执行 shell。
推荐各服务通过 `credentials_file` 指定私有环境文件，文件须归本人所有、权限 0600。
只支持普通 `KEY=value` / `export KEY=value`，不执行命令替换、变量展开或 source。

遥控使用 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_ALLOW_OPEN_IDS`，群聊还需
`FEISHU_BOT_OPEN_ID`。默认凭据路径 `~/.config/claude-feishu/env`；空白名单拒绝启动。
CLI 为已安装的 `claude`，也可通过 `claude_cli` 指定，不自动安装或更改 Claude 登录配置。

```bash
pixi run claude-feishu --once
pixi run claude-feishu
pixi run claude-feishu --stop
```

### 飞书里的用法

直接发指令即可执行；执行中每 5 秒原地刷新同一张进度卡片，显示思考摘要、当前动作、
最新输出（工具回显/报错）与最近步骤，收尾卡片带耗时、轮次、输出 token 与成本。

| 命令 | 作用 |
|---|---|
| `/help` | 列出全部命令与打断词 |
| `/new` | 开新会话（清空上下文，保留工作目录与累计统计） |
| `/status` | 会话状态：会话 id、目录、上下文占比、队列、累计 |
| `/cd <目录>` | 切换该会话的工作目录 |
| `/cost` | 本会话累计成本/轮次/时长 |
| `/queue <指令>` | 排队执行（当前任务结束后自动开始，上限 5 条） |

执行中发「停」「停止」「打断」「取消」「stop」「cancel」打断当前任务；发其他内容会
提示忙并建议用 `/queue`。命令与统计按 chat 隔离。

权限档由本机配置的 `permission_profile` 选择，新机器默认 `read-only`。
经用户明确授权，可设为 `workspace`，保留原遥控的 Read/Glob/Grep/Bash/Edit/Write/WebFetch
以及原危险命令 deny 列表。不会添加跳过权限检查的参数，也不接受任意 settings 路径。
这只是 Claude 工具权限规则，不是操作系统沙箱；Bash 具有当前用户权限，应仅对白名单可信用户开放。
不要在消息或上下文中暴露凭据。配置改变后先 `--stop` 再启动。
保留消息/event/fingerprint 防重放、过期消息拒绝、聊天会话映射、串行执行、超时进程组清理。

## 运行状态与迁移

每个服务独立写 `.local/tools/<服务名>/service.log`、`pid.json`、锁和游标。
遥控的 `seen.json` / `sessions.json` 只保留去重 ID 和原生会话映射，不共享聊天原文。
旧遥控服务的会话映射/防重放文件只能在旧服务停止后迁移，不同时运行两个消费者。
2026-09-07 评测机已完成切换：旧独立仓库归档至
`.local/migrations/claude-feishu-20260907/`（含 `.git`），不再作为运行依赖。
原状态文件保留作备份，当前服务只写 `.local/tools/claude-feishu/`。
`service.log` 的 `Feishu websocket connected` 表示 SDK 已完成连接，不打印带签名的连接 URL。

核心代码、模板与 Pixi lock 走 Git；凭据、日志、PID、游标仅本机；Memory/Skill/项目资料沿用 S3。
当前仅迁移 Pi 训练和 B1K 评测 watchdog，GOAI 的独立 watchdog 不在本轮范围内。

## 验证

```bash
pixi install -e dev --locked
pixi run -e dev test
pixi run -e dev lint
```

测试使用 mock 飞书、合成业务数据、隔离的临时服务；不访问 GPU，不启动训练/评测。
`b1k_layout.py` 是只读产物路径协议适配，不依赖 evaluator 模块；业务路径协议变动时须同步测试此适配。
