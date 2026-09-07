# Agent 工作区记忆核心

这是供 Codex、Claude Code 和 Kimi Code 共用的轻量级多 Agent 工作区记忆系统。

公共仓库只保存可复用代码；工作区专属的 Skill、Prompt、项目知识和记忆保存在私有 S3 兼容存储中。Git、源码、配置、测试结果和实际环境始终是真实来源，memory 只作为可追溯的导航和认知缓存。

## 存储边界

| 层级 | 内容 | 真实来源 |
|---|---|---|
| 公共核心 | `config.template.json`、`scripts/`、`tests/`、CI 和文档 | Git |
| 共享工作区状态（本地收纳于 `.agents/.share/`） | `config/`、`skills/`、`long-term/`、`shared_files/`、`memory/` | S3 |
| 原始记忆事件 | `memory/<machine>/<agent>/<UTC-date>.json` | S3 |
| 本机运行状态（本地收纳于 `.agents/.local/`，`config.json` 保留根目录） | `config.json`、`.locks/`、`.sync/` | 仅本机 |

原始事件按机器和 Agent 分区，每台机器只上传自己的前缀。共享状态采用完整目录同步，并使用本地基线检测本地与 S3 是否发生分叉。

项目只依赖 Python 标准库和 MinIO Client（`mc`），不需要数据库、MCP Server 或本地搜索服务。

## 公共仓库结构

```text
.
├── .github/workflows/tests.yml
├── config.template.json
├── scripts/
│   ├── memory.py
│   └── sync.py
└── tests/
```

`config.template.json` 随 Git 发布。执行 S3 初始化后，本地会出现 `.share/`（`config/`、`skills/`、`long-term/`、`memory/`、`shared_files/`）和 `.local/`（`.locks/`、`.sync/`）；这些工作区专属内容均被 Git 忽略，顶层只保留 `config.json`。

## S3 结构

```text
<remote>/
├── config/
│   └── prompts/
├── skills/
├── long-term/
├── shared_files/
└── memory/
    └── <machine>/<agent>/<UTC-date>.json
```

S3 目录与本地 `.share/` 下的目录一一对应（远端目录名不变，仅本地收纳位置统一在 `.share/` 下）。S3 不设置 release 目录，也不额外实现对象版本管理。Git 中的 `config.template.json` 是新机器初始化的唯一模板；它不包含 remote、项目名称或仓库路径。初始化时会基于它生成本机专属、不会上传的 `config.json`。

## 新机器初始化

前置条件：

- Python 3.10 或更高版本
- Git
- MinIO Client（`mc`），并已在本机配置私有 S3 alias
- 工作区根目录，例如 `~/code`

先克隆公共核心：

```bash
git clone <公共仓库地址> ~/code/.agents
```

再从 S3 初始化：

```bash
~/code/.agents/scripts/sync.py init --pull \
  --remote <mc-alias>/<bucket>/<prefix> \
  --machine-id <唯一机器名> \
  --machine-role <机器用途> \
  --workspace-root ~/code \
  --clear-proxy
```

只有负责发布共享状态的指定机器才添加 `--shared-writer`。复用机器 ID 前必须确认旧实例已经停止，并显式添加 `--reuse-machine`。

初始化过程会：

1. 读取 Git 中的 `config.template.json`，并下载 S3 shared 内容；
2. 生成本机 `config.json`；
3. 拉取其他机器的 raw memory；
4. 将全部共享 Skill 同源链接到 Codex、Claude 和 Kimi，遇到同名不同来源的已有 Skill 时拒绝覆盖；
5. 创建工作区 `AGENTS.md` 和 `CLAUDE.md` Prompt 链接；
6. 校验 memory 数据。

旧分类目录中的同名 Skill 副本不会盖过其嵌套规范入口。不同分类下无层级关系的
同名 Skill 会被拒绝，避免链接到不确定来源；不会覆盖 Agent 已有的独立配置。

公开模板不包含 `projects`。需要 repo 级记忆时，在本机 `config.json` 中按需加入项目注册表；该信息不会上传到公共 Git：

```json
{
  "projects": {
    "repo-name": {
      "path": "relative/path",
      "description": "仓库用途"
    }
  }
}
```

不配置 `projects` 时，workspace 级 memory 仍可正常使用。

## 常用命令

```bash
# 查询和校验 raw memory
python3 scripts/memory.py status
python3 scripts/memory.py recent --limit 20
python3 scripts/memory.py search "关键词"
python3 scripts/memory.py recent --project repo-name --machine machine-name --current --limit 10
python3 scripts/memory.py recent --since 2026-08-01 --until 2026-09-01 --limit 100
python3 scripts/memory.py get <event_id>
python3 scripts/memory.py audit --project repo-name
python3 scripts/memory.py validate

# 只上传本机 raw memory
python3 scripts/sync.py push-memory

# 日常同步 raw memory 和 shared
python3 scripts/sync.py sync

# 单独处理 shared
python3 scripts/sync.py pull-shared
python3 scripts/sync.py push-shared
python3 scripts/sync.py sync-shared
python3 scripts/sync.py resolve-shared

# 发布单个有明确所有权的共享文件，并拉取其他远端更新
python3 scripts/sync.py publish-shared-file \
  shared_files/project-docs/report.md
```

`--machine`、`--project`、`--task`、`--agent`、`--topic` 是精确筛选，关键词搜索为
所有词同时匹配。时间区间含起点、不含终点，日期按 UTC；带时区时间会换算后比较。
`--current` 排除显式 superseded 的事件，不代表已验证，也不会自动合并同 topic 的不同结论。
写周报/月报时保留历史，不加 `--current`。`get` 返回完整事件和真实来源路径。

`audit` 默认检查全部事件，支持相同筛选和 `--stale-days`（默认 60 天）。它只报告
缺来源、过期候选、悬空/循环 supersedes、并行取代候选及历史时间格式，不重写记忆。
正常查询成功返回 0；`get` 未找到、`audit` 有待核验项、`validate` 校验失败返回 1；
参数、I/O 或冲突错误返回 2。历史非 UTC 时间和日期错位保留原始来源，仅审计提示。

## Raw 一致性

- 同 ID、同内容导入幂等；同 ID、不同内容（包括不同机器或日期）拒绝，整批先检查再写入。
- 同机写入与安装共用短时本地锁，每个 daily JSON 原子替换；写入期间不等待 S3。
- 同机 push/pull 串行。push 上传已验证的 daily JSON 快照，不上传原子写临时文件；
  上传期间新增事件留到下一次 push，失败不删除本地事件。
- pull 先下载、完整校验，再原子安装每个文件。远端缺少已知事件或同 ID 内容变化时
  拒绝整批安装，`.local/.sync/raw-conflict.json` 只记录 ID，不备份事件正文。
- 正常 pull 不覆盖本机目录；恢复机器用 `init --pull --reuse-machine` 才导入本机历史。

同 ID 的脱敏修改也会触发保护。先核对产生机器、S3 和本地内容，得到明确授权后进行
定向脱敏或替换；不要自动 union、删除本地文件绕过检查，或复制凭据到冲突日志。
新事件仍使用 UTC，历史记录不自动搬家。

## 维护边界

继续使用 daily JSON + 中文 Markdown，不引入数据库/MCP，也不迁移已有事件。
Agent 日常只读相关摘要和少量事件，按 ID 取证；整理长期记忆时按项目分节，机器信息
写清 machine_id、证据日期和事件 ID。周报/月报使用时间筛选，不把运行状态当成永久事实。

工具代码升级走 Git，S3 只同步共享内容，不会自动把新 CLI 分发到其他机器；新 Skill
在旧 CLI 上应降级到原有查询。已有命令、S3 key 和目录兼容，无需批量迁移。

已知限制：本地锁不能协调两台机器复用同一个 machine_id；shared 基线比较不是远端
CAS 事务，检查后到上传期间仍有竞态，应遵守指定 writer/文件所有权，避免同时发布。
raw 仍全量下载，安装仅保证每个 JSON 原子性，不保证跨文件事务；中断后可重新同步。
secret 检查只是结构化敏感键和常见凭据模式的防线，不能证明自然语言一定不含密码。
本轮不自动删除、压缩或语义裁决历史事件；容量明显增长后再评估增量同步或检索索引。

## Shared 分叉保护

最近一次成功对齐的 shared 完整副本缓存在 `.local/.sync/base/`。同步前会比较三份内容：

1. 上次共同基线；
2. 当前本地 shared；
3. 当前 S3 shared。

每个文件按相对路径和 SHA256 比较。只有一侧变化时自动同步；本地和 S3 都相对基线发生变化时，不覆盖任何一方，而是将远端快照和差异报告保存到 `.local/.sync/conflict/`。

人工将 `.local/.sync/conflict/remote/` 合并到当前本地 shared 后运行：

```bash
python3 scripts/memory.py validate
python3 -m unittest discover -s tests -v
python3 scripts/sync.py resolve-shared
```

`resolve-shared` 上传前会再次检查 S3。如果人工合并期间远端再次变化，操作会停止并要求重新合并。

`--allow-non-writer` 仅用于用户明确授权的维护迁移，不会绕过分叉检测。

`publish-shared-file` 用于 request/report 这类分属不同机器维护的文件：只上传
指定文件，同时把远端其他文件合并到本地；若远端也修改了同一个文件则拒绝
覆盖。它不适合发布 Skill、long-term 或一组相互依赖的共享文件。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 安全约束

禁止向 Git、长期记忆或 raw memory 写入 API Key、Access Key、Token、密码、私钥、完整聊天记录或其他凭据。S3 凭据只保存在本机 `mc` 配置中。
