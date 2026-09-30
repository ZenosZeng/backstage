#!/usr/bin/env bash
#
# 规范重启 claude-feishu bridge（飞书遥控）。
#
# 为什么要有这个脚本：agent 手搓的重启反复翻在同样三件事上 ——
#
#   1) **从飞书触发的 agent 里直接重启会自杀。** 那种 agent 本身是 bridge 的
#      子进程，`--stop` 一发出就把它自己也带走了，重启做到一半人就没了。
#      用 `--detach`：脚本脱离当前会话，sleep 完再动手。
#
#   2) **固定 sleep 等不够。** `--start` 返回只代表 daemon 已 fork，距 websocket
#      真的连上还有几秒；这期间发的消息会丢。这里改为轮询就绪标记
#      （日志里的 "Feishu websocket connected"）。
#
#   3) **硬编码 PID / 解释器路径。** PID 早就变了，脚本静默失效；解释器路径换台
#      机器就没了。这里一律走 `--status --json` 和 `pixi` 入口。
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
WAIT=40          # 停/起各自的等待上限（秒）
LOG_TAIL=15

usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

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

svc() { ( cd "$AGENTS_ROOT" && "$PIXI" run -e "$PIXI_ENV" "$SERVICE" "$@" ); }

# 只解析 JSON，不依赖 jq
pid_of()  { python3 -c 'import json,sys;print(json.load(sys.stdin).get("pid") or "")'; }
run_of()  { python3 -c 'import json,sys;print("true" if json.load(sys.stdin).get("running") else "false")'; }

status_json() { svc --status --json 2>/dev/null || true; }
is_running()  { [ "$(status_json | run_of 2>/dev/null || echo false)" = "true" ]; }

# ---- 后台模式：脱离当前会话后再动手 ------------------------------------------
# 从飞书 agent 里调用时必须走这条，否则停 bridge 会连同调用者一起杀掉。
if [ "$DETACH" = 1 ]; then
    RUNDIR="${AGENTS_ROOT}/.local/tools/${SERVICE}"
    mkdir -p "$RUNDIR"
    RUNLOG="${RUNDIR}/restart.log"
    setsid nohup "$0" --delay "$DELAY" --wait "$WAIT" >>"$RUNLOG" 2>&1 &
    echo "${SERVICE}: 已在后台重启（delay=${DELAY}s），进度见 ${RUNLOG}"
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

OLD_PID="$(status_json | pid_of 2>/dev/null || true)"
LOG="$(status_json | python3 -c 'import json,sys;print(json.load(sys.stdin).get("log") or "")' 2>/dev/null || true)"
[ -z "$LOG" ] && LOG="${AGENTS_ROOT}/.local/tools/${SERVICE}/service.log"

echo "==> 重启 ${SERVICE}（当前 ${OLD_PID:-未运行}）"

# ---- 停 ---------------------------------------------------------------------
if [ -n "$OLD_PID" ]; then
    svc --stop >/dev/null 2>&1 || true
    for _ in $(seq 1 "$WAIT"); do
        is_running || break
        sleep 1
    done
    is_running && fail "旧进程（pid=${OLD_PID}）在 ${WAIT}s 内没退出；未强杀（service.py 拒绝杀未验证的进程）"
    echo "    旧进程已退出"
else
    echo "    原本就没在跑"
fi

# ---- 起 ---------------------------------------------------------------------
# 记下起跑前的日志行数，用来确认「这一次」的 ready 标记，而不是翻到上一次的
BEFORE=0
[ -f "$LOG" ] && BEFORE="$(wc -l <"$LOG" 2>/dev/null || echo 0)"

svc >/dev/null 2>&1 || fail "启动命令本身失败了"

# 进程起来 + websocket 连上，两件都算就绪
NEW_PID=""
READY=0
for _ in $(seq 1 "$WAIT"); do
    NEW_PID="$(status_json | pid_of 2>/dev/null || true)"
    if [ -n "$NEW_PID" ] && [ -f "$LOG" ] \
       && tail -n "+$((BEFORE + 1))" "$LOG" 2>/dev/null | grep -q "Feishu websocket connected"; then
        READY=1
        break
    fi
    sleep 1
done

[ "$READY" = 1 ] || fail "新进程起来了但 ${WAIT}s 内没等到 websocket 就绪${NEW_PID:+（pid=${NEW_PID}）}"

echo "==> 就绪 pid=${NEW_PID}（旧 ${OLD_PID:-无}）"
