# 飞书遥控：DSH harness 参考

[返回工具总览](../README.md) · [版本记录](../../docs/changelog.md)

本文件只讲 DSH（第二个 harness）的权限、限制与部署注意。命令、配置字段与凭据等通用说明
见[工具总览](../README.md#飞书凭据)。

## 调用方式

DSH 固定使用 `--profile headless --json -`，指令走 stdin，不暴露在 argv 中。
对齐的外部版本：`@deepseek-ai/dsh@0.2.0-rc.2`。

## 权限

`permission_profile=read-only` 显式注入 `DSH_PERMISSION_MODE=read-only`，
`workspace` 注入 `workspace-write`；不会使用 `danger-full-access`。

**必须显式注入**：DSH 自身的默认值是 `workspace-write`，比本工具的默认 `read-only` 宽松，
放任其取默认会静默放大权限。

两套权限机制**不等价**，不要当成同一件事：Claude 用工具级 allow/deny 规则，
DSH 用以当前 chat 工作目录为根的文件/bash 沙箱。DSH 的交互审批在没有应答者时拒绝执行，
因此部分操作可能被拒绝。

## 卡片与显示

DSH 卡片按 thinking/text commit 块刷新，有工具调用与结果，**不能逐字刷新**。
卡片两行：主标题 = 执行器 + 状态 + 耗时，副标题 = 模型 · 目录 · 上下文。
飞书 header 只支持 title 加一行 subtitle，三样信息只能合成一行副标题；
副标题被服务端拒绝时降级为正文前两行，信息不丢。

`claude_model_name` / `dsh_model_name` 配置显示名称。这两个字段只改变显示，不切换模型，
变更底层模型时需同步更新名称。Claude 未配置时读取事件/CLI 配置中的模型名；
DSH 不报告模型名，未配置时显示“模型未配置”。

DSH 的事件流不报上下文窗口，也无法测量单次成本。上下文取最后一步的输入量（不累加），
窗口由 `dsh_context_window` 声明：配置后与 Claude 一样显示百分比，未配置（0）只显示绝对量，
避免拿错窗口算出误导性占比。**该值必须与该 profile 实际解析到的模型一致**——
配错时百分比看起来和正确的一样可信。DSH 侧成本无法上报，`/cost` 会明确说明。

每步 token 累加到累计统计，事件截断时会提示用量可能不完整。退出码决定成功/失败，
`reason` 仅用于失败标签。需要减少重复 PATCH 时可调大 `progress_interval_seconds`。

## 超时与重试

DSH 没有 Claude 的 `--max-turns 200` 限制；`dsh_timeout_seconds` 独立控制超时，
默认 600 秒，超时或打断终止该任务进程组。DSH 单次成本无法从事件流测量。

会话丢失、目录不匹配或不可采纳时**仅重建一次**，其他错误不重试。
`/cd` 保留 DSH 会话 ID，目录不匹配会在下次执行时安全重建一次；
`/new` 的 epoch 防止运行中的任务把已清除的会话重新绑定。

## 凭据

DSH 默认从 `$DSH_HOME/.credentials.yaml`（默认 `~/.dsh`）读取凭据。
子进程保留基础白名单及 `DSH_HOME`、`TMPDIR`，不默认转发 API key 或飞书凭据。
**这属于环境卫生，不能阻止 agent 自己读取同一用户可读的文件。**

当前工作目录的 `.env` 也会被 DSH 信任并读取。已核对 0.2.0-rc.2 的凭据优先级：
启动环境 > `$DSH_HOME/.credentials.yaml` > 当前目录 `.env` > `$DSH_HOME/.env`。
私有文件缺失或未配置对应 key 时，会回退到目录 `.env`，因此仍需检查陈旧凭据。

不要让 web UI 或其他 bridge 同时续跑同一个 DSH 会话；跨进程追加日志没有独占保护。

## 部署与回滚

**部署新代码前必须先 `pixi run claude-feishu --stop`，部署后再启动。**
旧进程内存仍持有 v1 状态，运行中升级会让它把新文件覆盖回旧结构，丢失 DSH 字段。

会话文件升 v2：chat 保存目录与 harness，`sessions.claude` / `sessions.dsh` 分别保存原生会话 ID
与累计统计。首次读取 v1 时保留权限 0600 的 `sessions.json.v1.bak`，原子写入 v2，不保存聊天正文。
回滚代码可以启动，但旧代码无法读取嵌套会话 ID；需停服务并从 `.v1.bak` 恢复，
才能恢复迁移前的 Claude 上下文。

规范重启用 `scripts/restart-claude-feishu.sh`：从飞书触发的 agent 必须加 `--detach`
（那种 agent 是 bridge 的子进程，直接停会把自己杀掉）。
