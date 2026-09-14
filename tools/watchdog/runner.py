"""Shared watchdog scheduler. Monitors only observe external processes and files."""

from __future__ import annotations

import os
import socket
import threading
import time


def lifecycle(state: str, name: str) -> str:
    return f"**Watchdog** - {state}\n- 主机: {socket.gethostname()}\n- PID: {os.getpid()}\n- Monitor: {name}"


def run(
    monitor,
    notifier,
    stop: threading.Event,
    *,
    poll_seconds: float = 60,
    quiet_hours=None,
) -> int:
    quiet_hours = set(range(8)) | {13} if quiet_hours is None else set(quiet_hours)
    last_alert = {}
    last_hour = None
    notifier.send_card(
        "watchdog_started", "Watchdog 已启动", lifecycle("已启动", monitor.name)
    )
    print(
        f"watchdog started: pid={os.getpid()} monitor={monitor.name} poll={poll_seconds}s",
        flush=True,
    )
    try:
        while not stop.is_set():
            try:
                for alert in monitor.check_alerts():
                    now = time.monotonic()
                    # 生命周期/边沿事件（进程启动、结束、异常退出）只在状态真的变化时
                    # 产生，每次都算新事件，不能被 alert_interval 吃掉——否则「死亡 →
                    # 重启 → 很快再次死亡」只会发出第一张故障卡。限流只作用于持续型
                    # 告警（如 stalled：只要还停滞就每轮都报）。
                    # getattr 兜底：其他 monitor（如 train）的 Alert 可能没有该字段。
                    if getattr(alert, "lifecycle", False) or (
                        now - last_alert.get(alert.key, float("-inf"))
                        >= monitor.alert_interval
                    ):
                        if notifier.send_card(
                            alert.card_type, alert.title, alert.message
                        ):
                            last_alert[alert.key] = now
                local = time.localtime()
                hour = time.strftime("%Y-%m-%d-%H", local)
                if (
                    local.tm_min == 0
                    and local.tm_hour not in quiet_hours
                    and hour != last_hour
                ):
                    content = (
                        monitor.card_content("运行中")
                        if hasattr(monitor, "card_content") and monitor.active()
                        else monitor.status()
                    )
                    if content and (not hasattr(monitor, "active") or monitor.active()):
                        if notifier.send_card("heartbeat", "例行状态", content):
                            last_hour = hour
            except Exception as error:
                print(f"watchdog scan failed: {type(error).__name__}", flush=True)
            stop.wait(poll_seconds)
    finally:
        notifier.send_card(
            "watchdog_stopped", "Watchdog 已停止", lifecycle("已停止", monitor.name)
        )
        print("watchdog stopped", flush=True)
    return 0
