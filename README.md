# Backstage

**为多 Agent、多机器、多仓库开发提供共享记忆与独立工具。**

让 Codex、Claude Code 和 Kimi Code 在同一个工作区里协作：复用已确认的项目知识，
监控长时间运行的任务，并通过飞书访问 Claude。业务代码留在各自仓库，辅助工具统一管理。

本地目录建议仍使用 `~/code/.agents`；Backstage 是项目名，不要求重命名工作区。

[快速开始](#快速开始) · [记忆与同步](docs/memory.md) · [工具配置](tools/README.md) · [开发](#开发)

## 可以做什么

| 能力 | 用途 |
|---|---|
| 共享记忆 | 按机器、Agent 和日期保存 JSON 事件，按项目整理中文长期知识 |
| 多仓库上下文 | 一个稳定 task ID 关联多个 repo，保留文件、分支和验证依据 |
| 跨机器同步 | 通过 S3 兼容存储同步 Skill、Prompt、文档与记忆，检测分叉和事件回退 |
| 独立监控 | Pi 训练、B1K 评测的日志与进度监控，提供飞书状态和告警 |
| 飞书遥控 | 通过白名单访问 Claude：进度卡片（思考/工具输出/成本）、命令集、按 chat 排队与会话上下文，防重放与执行超时 |
| 统一运行环境 | Pixi 管理依赖；每个服务拥有独立的配置、进程和运行状态 |

监控适配器目前面向 Pi 和 B1K，并非通用日志解析器。接入其他业务需要实现对应适配。
记忆不是聊天归档，也不是最终事实来源：使用重要结论前，仍需核对代码、Git 和测试。

## 设计

```text
Codex / Claude Code / Kimi Code
              |
         Skill / CLI
              |
       Backstage (.agents)
       ├── 记忆：JSON 事件 + Markdown 长期知识
       ├── 工具：watchdog + Claude 飞书遥控
       └── 同步：本地校验 + S3 基线对账
```

| 边界 | 保存内容 | 管理方式 |
|---|---|---|
| 公共核心 | 工具、同步程序、配置模板、测试和依赖锁 | Git |
| 共享知识 | Skill、Prompt、项目文档、原始记忆 | 私有 S3，本地镜像在 `.share/` |
| 本机状态 | 机器配置、服务日志、PID、会话映射和同步基线 | 本机文件，不上传 |

记忆核心使用 Python 标准库与 MinIO Client，不依赖数据库、MCP 或向量服务。
工具使用独立 Pixi 环境，不借用训练或仿真环境。Watchdog 只读业务状态，不控制任务或修改评分。

## 版本

这是本工作区的功能版本约定，不等于任何单个脚本的接口版本，也不表示已创建同名 Git tag。

| 版本 | 日期 | 范围 |
|---|---|---|
| **v0.3.0 · 当前** | 2026-09-11 | 飞书遥控对齐终端：进度卡片显示思考/工具参数/工具输出/最近步骤，逐字流式，耗时与成本入卡；命令集 `/help /new /status /cd /cost /queue` 与按 chat 排队；修异常路径子进程清理、跨 chat 排队、会话落盘并发、排队 FIFO、上下文占比口径；回归 154 例 |
| **v0.2.0** | 2026-09-09 | Watchdog 从评测日志头识别当前 run 配置、失败告警按运行态分级；飞书进度上报间隔可配；对应回归测试 |
| **v0.1.0** | 2026-09-07 | Backstage 工作区建立：共享记忆与跨机同步、Pi/B1K watchdog、飞书遥控、Pixi 统一环境 |

## 快速开始

### 1. 安装核心

当前工具环境支持 **Linux x86-64**，需要 Git 和已安装的 Pixi。
只使用记忆脚本时可使用 Python 3.10+；S3 同步还需要 `mc` 及本机配置好的存储 alias。

将下方 `<repository-url>` 替换为实际仓库地址：

```bash
git clone <repository-url> ~/code/.agents
cd ~/code/.agents
pixi install --locked
pixi run tools-status
```

工具可独立使用，无需先配置 S3。安装不会自动启动监控或遥控，也不会初始化业务仓库。

### 2. 配置需要的工具

从 `tools/config/` 选择模板，放入 `.local/config/`，按自己的机器填写路径与凭据文件位置：

```bash
mkdir -p .local/config
cp tools/config/watchdog-b1k.example.json .local/config/watchdog-b1k.json
```

先做无通知的只读检查，再启动：

```bash
pixi run watchdog-b1k --once
pixi run watchdog-b1k
pixi run watchdog-b1k --status
pixi run watchdog-b1k --stop
```

| 服务 | 启动入口 | 前置条件 |
|---|---|---|
| B1K 评测监控 | `pixi run watchdog-b1k` | 对应评测日志、配置与结果目录 |
| Pi 训练监控 | `pixi run watchdog-train` | 对应训练日志；查询集群还需配置 `kubectl` |
| Claude 飞书遥控 | `pixi run claude-feishu` | 已安装并登录 Claude CLI，飞书应用凭据和用户白名单 |

服务默认在后台运行，SSH 断开后仍可工作；不会自动配置开机启动。
均支持 `--status`、`--stop`、`--once`、`--foreground`、`--config PATH`。
其中遥控的 `--once` 只检查配置，不会连接飞书或运行 Claude。

完整字段、通知设置和权限说明见 [工具配置](tools/README.md)。

### 3. 加入共享记忆工作区

已有私有 S3 工作区时，用唯一机器名加入；示例 remote 是 `mc` 的 alias/bucket/prefix，
不是直接填写 Access Key 的位置：

```bash
pixi run sync init --pull \
  --remote <mc-alias>/<bucket>/<prefix> \
  --machine-id dev-machine-01 \
  --machine-role development \
  --workspace-root ~/code
```

初始化生成本机 `config.json`，拉取共享文件，并为 Codex、Claude、Kimi 建立同源 Skill
链接及工作区 Prompt 链接。已有同名不同来源的文件不会被直接覆盖。

**公共仓库不附带你的 Skill、Prompt 和项目记忆。** 第一次创建共享存储，或需要配置
repo 注册表、指定共享 writer 时，请先阅读 [初始化说明](docs/memory.md#新机器初始化)。

## 日常使用

```bash
# 开始工作前同步，按需读取相关记忆
pixi run sync sync
pixi run memory recent --current --limit 10
pixi run memory search "关键词" --current --limit 10

# 查看完整来源或检查过期、冲突候选
pixi run memory get <event_id>
pixi run memory audit

# 发布本机新事件；查看工具运行状态
pixi run sync push-memory
pixi run tools-status
```

通过共享 Skill 约定 Agent 主动记录有价值的事实、决策和验证结果，不必等到会话结束。
原始事件按机器和 Agent 分区；长期知识是人工或 Agent 整理的 Markdown，不是自动裁决。

工具升级走 Git，知识同步走 S3。单独同步 Skill 不会安装新工具代码或升级 CLI。
查询筛选、首次发布和冲突恢复的完整说明见 [记忆与同步参考](docs/memory.md)。

## 目录

```text
.agents/
├── README.md
├── config.template.json       # 通用模板
├── pixi.toml / pixi.lock       # 环境与入口
├── scripts/                   # memory / sync
├── tools/                     # 服务管理、监控与遥控
├── tests/                     # 记忆和同步测试
├── docs/                      # 参考文档
├── config.json                # 本机配置，Git 忽略
├── .share/                    # 共享知识镜像，Git 忽略
└── .local/                    # 私有配置与运行状态，Git 忽略
```

## 安全与限制

- Secret、私钥、完整聊天和原始 Agent session 不应进入 Git 或共享记忆。凭据仅保留本机；敏感内容检测不等于绝对安全保证。
- 遥控默认 `read-only`。`workspace` 档允许 Bash、编辑和写文件，需明确授权且仅对可信用户开放；工具权限规则不是操作系统沙箱。
- 同机原子写入与文件锁不等于跨机器事务。机器 ID 必须唯一；共享内容遵守指定 writer 或文件所有权，避免同时发布。
- 同步发现分叉、同 ID 改写或已知事件缺失时拒绝静默覆盖。先核对证据，再按文档解决冲突。
- 当前为早期实现，raw memory 仍全量拉取，没有后台同步守护进程；服务状态和机器配置不跨机复制。

## 开发

```bash
pixi install -e dev --locked
pixi run -e dev test
pixi run -e dev lint
git diff --check
```

测试使用临时目录、合成日志和 mock 飞书，不需要 GPU、训练集群或真实 S3 账户。
修改业务日志适配器时请补充对应 fixture；修改同步或服务管理时请覆盖失败恢复与并发边界。
反馈问题时附上复现步骤和脱敏信息，不要提交 `.local/`、`.share/` 或凭据文件。

## 许可

仓库尚未附带 `LICENSE`，开源许可证待确定；公开可见不等同于已授予开源使用许可。
