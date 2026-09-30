#!/usr/bin/env bash
#
# 规范重启 claude-feishu bridge（飞书遥控）。
#
# agent 的 setsid 后代仍会继承 Landlock，不能负责启动新 bridge。
# 本脚本只向现有宿主机服务请求重启；由服务校验新代码后自行 re-exec。
# --detach 让调用者先完成飞书回复；--restart 会等待新实例与 websocket 就绪。
# 不会 stop/start，也不会把 agent 的权限模式改成 full-access。
#
# 用法：
#   scripts/restart-claude-feishu.sh                        # 立即重启，等到就绪
#   scripts/restart-claude-feishu.sh --delay 30             # 先等 30s（让当前回复先发出去）
#   scripts/restart-claude-feishu.sh --detach --delay 30    # 脱离会话后台跑，立刻返回
#
# 退出码：0 = 新 bridge 已就绪；1 = 失败（已打印日志尾部和手动命令）。
#
set -euo pipefail

AGENTS_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="claude-feishu"
PIXI_ENV="dev"

DELAY=0
DETACH=0
WAIT=60          # 校验与重新就绪的等待上限（秒）
LOG_TAIL=15

usage() { sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [ $# -gt 0 ]; do
    case "$1" in
        --delay)  DELAY="${2:?--delay 需要秒数}"; shift 2 ;;
        --detach) DETACH=1; shift ;;
        --wait)   WAIT="${2:?--wait 需要秒数}"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "未知参数：$1（-h 看用法）" >&2; exit 2 ;;
    esac
done

# ---- 工具定位（不硬编码 HOME/解释器）-----------------------------------------
PIXI="$(command -v pixi || true)"
[ -z "$PIXI" ] && [ -x "${HOME}/tools/pixi" ] && PIXI="${HOME}/tools/pixi"
if [ -z "$PIXI" ]; then
    echo "找不到 pixi（PATH 里没有，\$HOME/tools/pixi 也不存在）" >&2
    exit 1
fi

svc() { ( cd "$AGENTS_ROOT" && "$PIXI" run --as-is -e "$PIXI_ENV" "$SERVICE" "$@" ); }

case "$DELAY:$WAIT" in
    *[!0-9:]*|:*) echo "--delay/--wait 必须是非负整数" >&2; exit 2 ;;
esac
if [ "$WAIT" -lt 1 ] || [ "$WAIT" -gt 300 ]; then
    echo "--wait 必须为 1..300 秒" >&2
    exit 2
fi

# ---- 后台模式：脱离当前会话后再动手 ------------------------------------------
# 从飞书 agent 里调用时必须走这条，否则停 bridge 会连同调用者一起杀掉。
if [ "$DETACH" = 1 ]; then
    RUNDIR="${AGENTS_ROOT}/.local/tools/${SERVICE}"
    mkdir -p "$RUNDIR"
    RUNLOG="${RUNDIR}/restart.log"
    setsid nohup bash "$0" --delay "$DELAY" --wait "$WAIT" >>"$RUNLOG" 2>&1 &
    echo "${SERVICE}: 已提交后台重启请求（delay=${DELAY}s），进度见 ${RUNLOG}"
    exit 0
fi

fail() {
    echo "✗ $*" >&2
    echo "  日志尾部（${LOG}）：" >&2
    tail -n "$LOG_TAIL" "$LOG" 2>/dev/null | sed 's/^/    /' >&2 || true
    echo "  手动命令：cd ${AGENTS_ROOT} && ${PIXI} run -e ${PIXI_ENV} ${SERVICE}" >&2
    exit 1
}

[ "$DELAY" -gt 0 ] 2>/dev/null && { echo "等 ${DELAY}s 再动手…"; sleep "$DELAY"; }

LOG="${AGENTS_ROOT}/.local/tools/${SERVICE}/service.log"
echo "==> 请求 ${SERVICE} 在原宿主机上下文中重启"
svc --restart --restart-timeout "$WAIT" || fail "重启请求未完成；不会从当前调用者启动新 bridge"
echo "==> 新 bridge 与飞书 websocket 已就绪"
