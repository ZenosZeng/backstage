# Agent 工作区记忆核心

这是供 Codex 和 Claude Code 共用的轻量级多 Agent 工作区记忆系统。

公共仓库只保存可复用代码；工作区专属的 Skill、Prompt、项目知识和记忆保存在私有 S3 兼容存储中。Git、源码、配置、测试结果和实际环境始终是真实来源，memory 只作为可追溯的导航和认知缓存。

## 存储边界

| 层级 | 内容 | 真实来源 |
|---|---|---|
| 公共核心 | `config.template.json`、`scripts/`、`tests/`、CI 和文档 | Git |
| 共享工作区状态 | `config/`、`skills/`、`long-term/` | S3 |
| 原始记忆事件 | `memory/<machine>/<agent>/<UTC-date>.json` | S3 |
| 本机运行状态 | `config.json`、`.locks/`、`.sync/` | 仅本机 |

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

`config.template.json` 随 Git 发布。执行 S3 初始化后，本地会出现 `skills/`、`long-term/` 和 `config/`；这些工作区专属内容均被 Git 忽略。

## S3 结构

```text
<remote>/
├── config/
│   └── prompts/
├── skills/
├── long-term/
└── memory/
    └── <machine>/<agent>/<UTC-date>.json
```

S3 不设置 release 目录，也不额外实现对象版本管理。Git 中的 `config.template.json` 是新机器初始化的唯一模板；它不包含 remote、项目名称或仓库路径。初始化时会基于它生成本机专属、不会上传的 `config.json`。

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
4. 将全部共享 Skill 同源链接到 Codex 和 Claude，遇到同名不同来源的已有 Skill 时拒绝覆盖；
5. 创建工作区 `AGENTS.md` 和 `CLAUDE.md` Prompt 链接；
6. 校验 memory 数据。

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
```

## Shared 分叉保护

最近一次成功对齐的 shared 完整副本缓存在 `.sync/base/`。同步前会比较三份内容：

1. 上次共同基线；
2. 当前本地 shared；
3. 当前 S3 shared。

每个文件按相对路径和 SHA256 比较。只有一侧变化时自动同步；本地和 S3 都相对基线发生变化时，不覆盖任何一方，而是将远端快照和差异报告保存到 `.sync/conflict/`。

人工将 `.sync/conflict/remote/` 合并到当前本地 shared 后运行：

```bash
python3 scripts/memory.py validate
python3 -m unittest discover -s tests -v
python3 scripts/sync.py resolve-shared
```

`resolve-shared` 上传前会再次检查 S3。如果人工合并期间远端再次变化，操作会停止并要求重新合并。

`--allow-non-writer` 仅用于用户明确授权的维护迁移，不会绕过分叉检测。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 安全约束

禁止向 Git、长期记忆或 raw memory 写入 API Key、Access Key、Token、密码、私钥、完整聊天记录或其他凭据。S3 凭据只保存在本机 `mc` 配置中。
