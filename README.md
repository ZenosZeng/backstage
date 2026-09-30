# Backstage

**为多 Agent、多机器、多仓库开发提供共享记忆、任务监控与飞书遥控。**

Codex、Claude Code 和 Kimi 共用可追溯的知识，业务代码留在各自仓库。
本地目录建议使用 `~/code/.agents`；Backstage 是项目名，不要求重命名工作区。

[快速开始](#快速开始) · [日常使用](#日常使用) · [常用配置](#常用配置) · [记忆参考](docs/memory.md) · [工具参考](tools/README.md) · [版本记录](docs/changelog.md)

## 当前版本

**v0.4.0**：飞书遥控新增第二个 harness DSH（`/claude` · `/dsh` 粘性切换，各存一条原生会话），
卡片改为两行，新增规范重启脚本。历史变化、升级注意事项与旧编号映射见 [版本记录](docs/changelog.md)。

## 主要功能

### 通用能力

| 功能 | 用途 |
|---|---|
| 共享记忆 | raw JSON 供 Agent 读写；`knowledge` 用中文 Markdown、必要时配 SVG，供人阅读和 Agent 导航 |
| 跨仓库任务 | 稳定 task ID 关联多个 repo，记录事实、决策、来源与任务状态 |
| S3 同步 | 同步 Skill、Prompt、知识与记忆，检测分叉和事件回退 |
| 飞书遥控 | 白名单访问 Claude，按 chat 管理上下文和排队，回传进度与结果 |
| 监控基础设施 | 服务启停、状态轮询、飞书通知与告警去重；业务数据由适配器解析 |

### 业务适配

| 适配器 | 用途 |
|---|---|
| Pi 训练监控 | 只读训练日志及集群状态，报告 step、loss、ETA 和故障 |
| B1K 评测监控 | 只读评测日志及结果，报告进度、结果和故障 |

飞书遥控不依赖 Pi/B1K；两个监控适配器不控制实际训练或评测任务。
接入其他业务需实现对应适配器，不能直接套用 Pi/B1K 的日志与状态规则。

公共核心、模板、测试和依赖锁走 **Git**；工作区专属内容走 **私有 S3**，镜像在 `.share/`。
凭据、服务状态和本机配置不共享。记忆是导航，重要结论须核对源码、Git 和测试。

## 快速开始

### 安装

工具环境支持 **Linux x86-64**，需要 Git 和 Pixi。仅使用记忆脚本需要 Python 3.10+；
S3 同步还需要 MinIO Client（`mc`）及本机配置的存储 alias。

```bash
git clone <repository-url> ~/code/.agents
cd ~/code/.agents
pixi install --locked
pixi run tools-status
```

安装不会自动启动服务或初始化业务仓库。工具可独立使用，无需先配置 S3。

### 加入共享工作区

已有私有 S3 工作区时，用唯一机器名初始化：

```bash
pixi run sync init --pull \
  --remote <mc-alias>/<bucket>/<prefix> \
  --machine-id dev-machine-01 \
  --machine-role development \
  --workspace-root ~/code
```

初始化生成本机 `config.json`，拉取共享内容，建立 Agent Skill 与工作区 Prompt 链接，
不覆盖已有同名不同来源的文件。公共仓库不附带私人 Skill、Prompt 或项目记忆。
首次创建共享存储、注册 repo 和指定 writer，见 [初始化说明](docs/memory.md#新机器初始化)。

### 配置并启动工具

从 `tools/config/` 选择模板复制到 `.local/config/`，填写本机路径及凭据文件位置：

```bash
mkdir -p .local/config
cp tools/config/watchdog-b1k.example.json .local/config/watchdog-b1k.json
pixi run watchdog-b1k --once  # 无通知的只读检查
pixi run watchdog-b1k         # 后台启动
```

| 服务 | 入口 | 前置条件 |
|---|---|---|
| B1K 评测监控 | `pixi run watchdog-b1k` | 评测日志、配置和结果目录 |
| Pi 训练监控 | `pixi run watchdog-train` | 训练日志；查询集群需配置 `kubectl` |
| Claude 飞书遥控 | `pixi run claude-feishu` | 已登录 Claude CLI、飞书应用凭据及非空用户白名单 |

均支持 `--status`、`--stop`、`--once`、`--foreground`、`--config PATH`。
遥控的 `--once` 只检查配置，不连接飞书或运行 Claude。服务默认后台运行，不自动配置开机启动；
配置改变后先停止再启动。完整设置见 [工具参考](tools/README.md)。

## 日常使用

### Agent 记忆

在 `~/code/.agents` 执行；共享 Skill 应让 Agent 主动读取和更新，不要求用户逐次提醒。

```bash
pixi run sync sync
pixi run memory brief --project repo-name
pixi run memory search "接口 契约" --current --limit 10
pixi run memory get <event_id>
pixi run memory recent --current --summary --limit 10
pixi run memory audit --project repo-name
pixi run sync push-memory
```

`brief` 只摘录、不改长期知识；默认隐藏暂停/关闭任务，显式 `--task <id>` 可查历史。
任务状态用 `memory task` 追加，恢复为 `active`，暂停为 `paused`，完成为 `closed`：

```bash
pixi run memory task --task release-check --state active --agent codex \
  --project repo-a --project repo-b --reason "开始跨仓库验证" --where "需求单 #123"
```

状态不控制实际 job 或服务，也不是执行授权。并发冲突、生命周期示例和查询语义见
[记忆参考](docs/memory.md#精简上下文与任务生命周期)。工具升级走 Git，知识同步走 S3；同步 Skill 不会升级 CLI。

### 飞书指令

以下命令发给已启动的 Claude 飞书机器人，**不是终端命令**：

| 指令 | 作用 |
|---|---|
| `/help` | 查看命令与打断词 |
| `/claude` / `/dsh` | 粘性切换 harness，分别保存上下文与统计 |
| `/new [claude\|dsh\|all]` | 清空指定 harness 会话，保留工作目录与累计统计 |
| `/status` | 查看当前 harness、两套会话、目录、上下文和队列 |
| `/cd <目录>` | 切换该会话工作目录 |
| `/cost` | 查看当前 harness 累计轮次、输出 token 和时长 |
| `/queue <指令>` | 当前任务结束后继续执行，上限 5 条 |

直接发送普通文本可执行任务；执行中可发「停」「停止」「打断」「取消」「stop」「cancel」打断。
命令与统计按 chat 隔离。权限、凭据与运行状态见 [飞书说明](tools/README.md#飞书凭据)。

### 命令行帮助

```bash
pixi run memory --help
pixi run memory brief --help
pixi run sync --help
pixi run claude-feishu --help
pixi run tools-status
```

## 常用配置

记忆配置位于本机 `config.json`；服务配置位于 `.local/config/<服务名>.json`。
不要在公共配置或共享记忆中写凭据。

| 配置位置 | 常用参数 | 默认值或说明 |
|---|---|---|
| 记忆 | `machine_id`、`machine_role`、`workspace_root` | 初始化时指定；机器 ID 必须唯一 |
| 记忆 | `sync.remote`、`sync.clear_proxy` | S3 alias/bucket/prefix；清代理默认 `true` |
| 记忆 | `shared_writer`、`projects` | writer 默认 `false`；项目注册表按需添加，模板不带项目 |
| `brief` | `--limit`、`--summary-chars`、`--max-chars` | 默认 20 组、单条 240 字符、文本总计 6000 字符 |
| `search` | `--sort`、`--full` | 默认相关性排序和短摘要；`--sort recent --full` 查看时间倒序全文 |
| Watchdog | `repo_root`、`log` / `log_root` | 按服务指定业务仓库和日志位置 |
| Watchdog | `poll_seconds`、`quiet_hours` | 默认 60 秒；0–7 点和 13 点例行消息静默，告警不静默 |
| 遥控 | `workdir`、`claude_cli`、`credentials_file` | 工作目录、Claude 可执行文件和本机私有凭据文件 |
| 遥控 | `timeout_seconds`、`permission_profile` | 默认 600 秒、`read-only`；`workspace` 档需明确授权 |
| 遥控 | `progress_interval_seconds` | 卡片刷新默认 5 秒；0 关闭中间刷新，保留开始/结束卡片 |

完整字段与同步流程见 [记忆参考](docs/memory.md)；通知、停滞阈值及凭据设置见 [工具参考](tools/README.md#本机配置)。

## 安全与限制

- Secret、完整聊天和原始 session 不进入 Git 或共享记忆；敏感内容检测不是绝对安全保证。
- 遥控工具权限不是操作系统沙箱；`workspace` 档仅对可信白名单用户开放。
- 同机锁不等于跨机事务；shared 遵守指定 writer/文件所有权，分叉须核对后合并。
- raw memory 仍全量拉取，无后台同步守护进程；服务运行状态不跨机复制。
- Watchdog 只读业务状态，目前适配 Pi/B1K；接入其他业务需要对应适配器。

## 开发

```bash
pixi install -e dev --locked
pixi run -e dev test
pixi run -e dev lint
git diff --check
```

`scripts/` 放记忆/同步工具，`tools/` 放监控/遥控，`docs/` 放参考与版本记录。
测试使用临时目录、合成日志和 mock 飞书，无需 GPU 或真实 S3。修改行为须覆盖回归；
不要提交 `config.json`、`.share/`、`.local/` 或凭据。业务日志适配变更须补对应 fixture。

## 许可

仓库尚未附带 `LICENSE`，开源许可证待确定；公开可见不等于已授予开源使用许可。
