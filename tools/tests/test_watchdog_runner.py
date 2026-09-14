"""Watchdog 调度器的告警限流回归测试（不依赖飞书、不启服务）。

背景（2026-09-14 审计 P2）：runner 原先按 alert.key 对**所有**告警统一限流
（默认 600s）。但生命周期事件（进程启动 / 结束 / 异常退出）是边沿触发的——
只在状态真的变化时产生，每次都算新事件。统一限流会把它们一起吃掉：
「死亡 → 重启 → 120 秒内再次死亡」只会发出第一张故障卡，第二次静默丢失。
现在只有持续型告警（如 stalled：只要还停滞就每轮都报）参与限流。
"""

from __future__ import annotations

import threading
import unittest

from tools.watchdog import runner


class _Alert:
    """最小 Alert 替身：只带 runner 用到的字段。"""

    def __init__(self, key: str, *, lifecycle: bool) -> None:
        self.key = key
        self.severity = "critical"
        self.card_type = "failed"
        self.title = key
        self.message = f"{key} message"
        self.lifecycle = lifecycle


class _Monitor:
    """每次轮询都返回同一批告警——模拟「条件持续存在」。

    停止条件是**轮询次数**而不是「发出多少条」：限流用例本来就只发一条，
    按发送数设停止条件会让循环永远停不下来（第一版就这么挂死的）。
    """

    name = "fake"
    alert_interval = 600

    def __init__(self, alerts: list, stop: threading.Event, polls: int = 4) -> None:
        self._alerts = alerts
        self._stop = stop
        self._polls = polls
        self.seen = 0

    def check_alerts(self) -> list:
        self.seen += 1
        if self.seen >= self._polls:
            self._stop.set()
        return list(self._alerts)

    def active(self) -> bool:
        return False  # 跳过整点心跳，避免干扰计数


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_card(self, card_type: str, title: str, message: str) -> bool:
        self.sent.append(title)
        return True


def _run(alerts: list, polls: int = 4) -> list[str]:
    stop = threading.Event()
    notifier = _Notifier()
    runner.run(_Monitor(alerts, stop, polls), notifier, stop, poll_seconds=0.01)
    # 首尾各有一条固定的 "Watchdog 已启动/已停止"，与本次断言无关
    return [t for t in notifier.sent if t not in ("Watchdog 已启动", "Watchdog 已停止")]


class LifecycleAlertTests(unittest.TestCase):
    def test_lifecycle_alert_is_not_throttled(self) -> None:
        """边沿事件每轮都要发出：三次死亡就是三张卡，不能被 600s 限流吃掉。"""
        titles = _run([_Alert("eval-process-died", lifecycle=True)])
        self.assertGreaterEqual(
            len(titles), 3, "生命周期事件被 alert_interval 限流了（审计 P2 回归）"
        )
        self.assertTrue(all(title == "eval-process-died" for title in titles))

    def test_lifecycle_mixed_with_persistent(self) -> None:
        """混在一起时，生命周期照发，持续型只发一次。"""
        titles = _run(
            [
                _Alert("eval-process-died", lifecycle=True),
                _Alert("eval-stalled-critical", lifecycle=False),
            ]
        )
        self.assertGreaterEqual(len([t for t in titles if t == "eval-process-died"]), 3)
        self.assertEqual(len([t for t in titles if t == "eval-stalled-critical"]), 1)

    def test_persistent_alert_still_throttled(self) -> None:
        """限流本身不能被改坏：持续型告警在 alert_interval 内只发一次。"""
        titles = _run([_Alert("eval-stalled-critical", lifecycle=False)])
        self.assertEqual(len(titles), 1)

    def test_alert_without_lifecycle_field_defaults_to_throttled(self) -> None:
        """没有 lifecycle 字段的旧 Alert（如 train 侧）按限流处理，不炸。"""

        class Legacy:
            key = "legacy-alert"
            severity = "warning"
            card_type = "warning"
            title = "legacy"
            message = "legacy message"

        titles = _run([Legacy()])
        self.assertEqual(len(titles), 1)


if __name__ == "__main__":
    unittest.main()
