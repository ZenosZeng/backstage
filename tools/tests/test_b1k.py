from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.watchdog import b1k as monitor_b1k
from tools.watchdog import notifier


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class B1kMonitorTests(unittest.TestCase):
    """Log-driven tests for the B1K monitor (eval_status.json removed 2026-08-12)."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.log_path = self.root / "eval.log"
        self.running = True
        self.clock = Clock()

    def tearDown(self):
        self.temporary.cleanup()

    def write_log(self, *lines: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as stream:
            for line in lines:
                stream.write(line + "\n")

    def monitor(self):
        return monitor_b1k.B1kEvalMonitor(
            self.log_path,
            process_checker=lambda: self.running,
            clock=self.clock,
            warning_seconds=1800,
            critical_seconds=3600,
        )

    HEARTBEAT = (
        "[12:00:00] Heartbeat: completed=10/200, running=14, idle=0, queued=0, "
        "failures=0, elapsed=00:01:00, ETA=01:00:00"
    )
    RESULTS = "Current results: rollouts=10 SR=2/10 Q=0.250"

    def test_cloud_topology_log_and_process_are_monitored(self):
        self.assertTrue(
            any("0srv16sim/eval.log" in str(path) for path in monitor_b1k.DEFAULT_LOGS)
        )
        self.assertIn(
            "scripts/eval/0srv16sim/eval.py",
            monitor_b1k.DEFAULT_PROCESS_MARKERS,
        )

    def test_fleet_log_and_process_are_monitored(self):
        self.assertIn("scripts/eval/fleet/eval.py", monitor_b1k.DEFAULT_PROCESS_MARKERS)
        self.assertTrue(
            any("fleet/eval.log" in str(path) for path in monitor_b1k.DEFAULT_LOGS)
        )
        monitor = monitor_b1k.B1kEvalMonitor(
            repo_root=self.root, process_checker=lambda: True
        )
        self.assertIn(self.root / "scripts/eval/fleet/eval.log", monitor.default_logs)

    def test_stall_warning_and_critical(self):
        monitor = self.monitor()
        self.write_log(self.HEARTBEAT)
        monitor.check_alerts()  # 首检建立基线（不触发 started）
        self.clock.value = 1801
        alerts = monitor.check_alerts()
        self.assertIn("eval-stalled-warning", [alert.key for alert in alerts])
        self.assertIn("stalled", [alert.card_type for alert in alerts])
        self.clock.value = 3601
        self.assertIn(
            "eval-stalled-critical", [alert.key for alert in monitor.check_alerts()]
        )

    def test_fleet_stall_defaults_and_minute_checks(self):
        self.log_path = self.root / "fleet" / "eval.log"
        self.log_path.parent.mkdir()
        monitor = monitor_b1k.B1kEvalMonitor(
            self.log_path, clock=self.clock, process_checker=lambda: True
        )
        self.assertEqual(monitor.alert_interval, 600)
        self.write_log(self.HEARTBEAT)
        monitor.check_alerts()
        self.clock.value = 901
        self.assertIn("eval-stalled-warning", [a.key for a in monitor.check_alerts()])
        self.clock.value = 3601
        self.assertIn("eval-stalled-critical", [a.key for a in monitor.check_alerts()])

    def test_alert_interval_is_configurable(self):
        """告警重发间隔可经配置覆盖，默认 600 秒（2026-09-13 由 60 调高以降低推送频率）。"""
        default = monitor_b1k.B1kEvalMonitor(
            self.root / "fleet" / "eval.log", process_checker=lambda: True
        )
        self.assertEqual(default.alert_interval, 600)
        overridden = monitor_b1k.B1kEvalMonitor(
            self.root / "fleet" / "eval.log",
            process_checker=lambda: True,
            alert_interval_seconds=1800,
        )
        self.assertEqual(overridden.alert_interval, 1800)
        with self.assertRaises(ValueError):
            monitor_b1k.B1kEvalMonitor(
                self.root / "fleet" / "eval.log",
                process_checker=lambda: True,
                alert_interval_seconds=0,
            )

    def test_failure_increase_and_process_death(self):
        monitor = self.monitor()
        self.write_log(self.HEARTBEAT)
        monitor.check_alerts()
        self.write_log(
            "[12:05:00] Heartbeat: completed=10/200, running=14, idle=0, queued=0, "
            "failures=2, elapsed=00:06:00, ETA=01:00:00"
        )
        alerts = monitor.check_alerts()
        self.assertIn("eval-failures-increased", [alert.key for alert in alerts])
        warning = next(alert for alert in alerts if alert.key == "eval-failures-increased")
        self.assertEqual(warning.card_type, "warning")
        self.assertEqual(warning.severity, "warning")
        self.running = False
        died = next(alert for alert in monitor.check_alerts() if alert.key == "eval-process-died")
        self.assertEqual(died.card_type, "failed")

    def test_process_completion_uses_finished_card(self):
        monitor = self.monitor()
        self.write_log(self.HEARTBEAT)
        monitor.check_alerts()
        self.write_log(
            "Evaluation finished: status=complete exit_code=0 total=01:00:00"
        )
        self.running = False
        alerts = monitor.check_alerts()
        finished = next(alert for alert in alerts if alert.key == "eval-finished")
        self.assertEqual(finished.card_type, "finished")

    def test_log_keyword(self):
        monitor = self.monitor()
        monitor.check_alerts()
        self.write_log("Evaluation failed: CUDA out of memory")
        self.assertIn("eval-log-error", [alert.key for alert in monitor.check_alerts()])

    def test_log_heartbeat_parsed(self):
        monitor = self.monitor()
        self.write_log(self.HEARTBEAT)
        heartbeat = monitor._log_heartbeat()
        self.assertEqual(heartbeat["completed_rollouts"], 10)
        self.assertEqual(heartbeat["total_rollouts"], 200)
        self.assertEqual(heartbeat["failed_rollouts"], 0)
        self.assertEqual(heartbeat["eta"], "01:00:00")

    def test_log_config_overrides_stale_watchdog_and_results(self):
        checkpoint = self.root / "ckpts/exp/step70000/ema"
        configs = []
        for name in ("raw", "q95"):
            path = self.root / f"{name}.toml"
            path.write_text(
                f"# request_id = \"stale-comment\"\n[request]\nrequest_id = '{name}'\n"
                f'[defaults]\noutput_root = "{self.root / name}"\n'
                f'[[jobs]]\ncheckpoint = "{checkpoint}"\ntask = "radio"\n'
            )
            configs.append(path)
        old_summary = self.root / "raw/exp/step70000/ema/radio/summary.json"
        old_summary.parent.mkdir(parents=True)
        old_summary.write_text(json.dumps({"completed_rollouts": 120, "successes": 19, "mean_q_score": 0.225}))
        monitor = monitor_b1k.B1kEvalMonitor(self.log_path, config_path=configs[0])
        self.write_log(f"Config: {configs[1]}", "x" * (300 * 1024),
                       "Progress: completed=21/880, failures=21, ETA=estimating")
        content = monitor.card_content("存在失败")
        self.assertIn("**q95**", content)
        self.assertNotIn("19/120", content)
        self.assertIn("结果：暂无", content)
        self.assertIn("已结束尝试：21/880", content)
        self.assertIn("有效完成：0", content)
        self.assertIn("运行失败：21", content)
        self.assertIn("有效完成：0", monitor.status())
        self.assertIn("有效完成：0", monitor._started_content())
        # The monitor follows a replaced log without restarting its process.
        self.log_path.write_text(f"Config: {configs[0]}\n{self.HEARTBEAT}\n")
        self.assertEqual(monitor._toml_request_id(), "raw")

    def test_missing_log_config_never_falls_back_to_old_request(self):
        old = self.root / "old.toml"
        old.write_text('[request]\nrequest_id = "old"\n')
        monitor = monitor_b1k.B1kEvalMonitor(self.log_path, config_path=old)
        missing = self.root / "missing.toml"
        self.write_log(f"Config: {missing}", self.HEARTBEAT)
        self.assertEqual(monitor._eval_toml_path(), missing)
        self.assertEqual(monitor._toml_request_id(), "")
        self.assertEqual(monitor._per_checkpoint_results(), [])

    def test_relative_log_config_resolves_against_repo(self):
        monitor = monitor_b1k.B1kEvalMonitor(self.log_path, repo_root=self.root)
        self.write_log("Config: some directory/run.toml", self.HEARTBEAT)
        self.assertEqual(monitor._eval_toml_path(), self.root / "some directory/run.toml")

    def test_successful_and_failed_attempts_are_separate(self):
        self.write_log("Progress: completed=25/880, failures=21, ETA=estimating")
        self.assertIn("有效完成：4", self.monitor().card_content("运行中"))

    def test_card_content_has_b1k_metrics(self):
        old_repo = monitor_b1k.REPO_ROOT
        try:
            monitor_b1k.REPO_ROOT = self.root
            (self.root / "scripts" / "eval").mkdir(parents=True)
            (self.root / "scripts" / "eval" / "eval.toml").write_text(
                f'[request]\nrequest_id = "test"\n[defaults]\n'
                f'output_root = "{self.root / "evals"}"\n',
                encoding="utf-8",
            )
            monitor = self.monitor()
            self.write_log(self.HEARTBEAT)
            content = monitor.card_content("运行中")
            self.assertIn("10/200 rollouts", content)
            self.assertIn("结果：暂无", content)
        finally:
            monitor_b1k.REPO_ROOT = old_repo

    def test_card_content_lists_distinct_checkpoint_sr_q(self):
        old_repo = monitor_b1k.REPO_ROOT
        try:
            monitor_b1k.REPO_ROOT = self.root
            toml_dir = self.root / "scripts" / "eval"
            toml_dir.mkdir(parents=True)
            evals = self.root / "evals"
            (toml_dir / "eval.toml").write_text(
                f'[request]\nrequest_id = "test"\n'
                f'[defaults]\noutput_root = "{evals}"\n'
                f'[[jobs]]\ncheckpoint_id = "dual-30k"\n'
                f'checkpoint = "{self.root / "ckpts" / "0826_dual" / "step30000" / "ema"}"\n'
                f'task = "turning_on_radio"\n'
                f'[[jobs]]\ncheckpoint_id = "dual-30k"\n'
                f'checkpoint = "{self.root / "ckpts" / "0826_dual" / "step30000" / "ema"}"\n'
                f'task = "installing_a_modem"\n'
                f'[[jobs]]\ncheckpoint_id = "other-30k"\n'
                f'checkpoint = "{self.root / "ckpts" / "0826_other" / "step30000" / "ema"}"\n'
                f'task = "turning_on_radio"\nrun_namespace = "r2"\n',
                encoding="utf-8",
            )
            rows = [
                ("0826_dual", "turning_on_radio", 3, 0.30),
                ("0826_dual", "installing_a_modem", 9, 0.90),
                ("0826_other", "turning_on_radio/r2", 4, 0.40),
            ]
            for experiment, task, successes, mean_q in rows:
                directory = evals / experiment / "step30000" / "ema" / task
                directory.mkdir(parents=True)
                (directory / "summary.json").write_text(
                    json.dumps(
                        {
                            "successes": successes,
                            "completed_rollouts": 10,
                            "mean_q_score": mean_q,
                        }
                    ),
                    encoding="utf-8",
                )

            content = self.monitor().card_content("运行中")
            self.assertIn("dual-30k: SR 12/20 Q 0.600", content)
            self.assertIn("other-30k/r2: SR 4/10 Q 0.400", content)
            self.assertEqual(content.count("SR 12/20"), 1)
        finally:
            monitor_b1k.REPO_ROOT = old_repo

    def test_results_honor_custom_config_and_resume_outputs(self):
        old_repo = monitor_b1k.REPO_ROOT
        old_config = os.environ.get("B1K_EVAL_CONFIG")
        try:
            monitor_b1k.REPO_ROOT = self.root
            evals = self.root / "evals"
            config = self.root / "custom.toml"
            checkpoint = self.root / "ckpts" / "exp-a" / "step50000" / "ema"
            config.write_text(
                f'[defaults]\noutput_root = "{evals}"\nrun_namespace = "r3"\n'
                f'[[jobs]]\ncheckpoint_id = "exp-a-50k"\n'
                f'checkpoint = "{checkpoint}"\ntask = "turning_on_radio"\n',
                encoding="utf-8",
            )
            summary = (
                evals
                / "exp-a"
                / "step50000"
                / "ema"
                / "turning_on_radio"
                / "r3"
                / "summary.json"
            )
            summary.parent.mkdir(parents=True)
            summary.write_text(
                json.dumps(
                    {"successes": 2, "completed_rollouts": 10, "mean_q_score": 0.25}
                ),
                encoding="utf-8",
            )
            os.utime(summary, (1, 1))
            os.environ["B1K_EVAL_CONFIG"] = str(config)
            self.write_log("[12:00:00] Evaluation started: request=test")

            content = self.monitor().card_content("运行中")
            self.assertIn("exp-a-50k/r3: SR 2/10 Q 0.250", content)
        finally:
            monitor_b1k.REPO_ROOT = old_repo
            if old_config is None:
                os.environ.pop("B1K_EVAL_CONFIG", None)
            else:
                os.environ["B1K_EVAL_CONFIG"] = old_config

    def test_cloud_release_results_are_aggregated_from_cloud_layout(self):
        old_repo = monitor_b1k.REPO_ROOT
        old_config = os.environ.get("B1K_EVAL_CONFIG")
        try:
            monitor_b1k.REPO_ROOT = self.root
            evals = self.root / "evals"
            config = self.root / "cloud.toml"
            release = self.root / "release.json"
            digest = "sha256:" + "a" * 64
            release.write_text(
                json.dumps({"model_id": "cloud-model-v1", "model_digest": digest}),
                encoding="utf-8",
            )
            config.write_text(
                f'[defaults]\nmodel_type = "cloud"\noutput_root = "{evals}"\n'
                f'[[jobs]]\ncheckpoint_id = "release-001"\n'
                f'release_config = "{release}"\ntask = "turning_on_radio"\n'
                f'[[jobs]]\ncheckpoint_id = "release-001"\n'
                f'release_config = "{release}"\ntask = "installing_a_modem"\n',
                encoding="utf-8",
            )
            for task, successes, mean_q in (
                ("turning_on_radio", 3, 0.3),
                ("installing_a_modem", 5, 0.5),
            ):
                summary = (
                    evals
                    / "cloud"
                    / "cloud-model-v1"
                    / ("a" * 16)
                    / task
                    / "summary.json"
                )
                summary.parent.mkdir(parents=True)
                summary.write_text(
                    json.dumps(
                        {
                            "successes": successes,
                            "completed_rollouts": 10,
                            "mean_q_score": mean_q,
                        }
                    ),
                    encoding="utf-8",
                )
            os.environ["B1K_EVAL_CONFIG"] = str(config)

            content = self.monitor().card_content("运行中")

            self.assertIn("release-001: SR 8/20 Q 0.400", content)
            self.assertEqual(content.count("release-001: SR"), 1)
        finally:
            monitor_b1k.REPO_ROOT = old_repo
            if old_config is None:
                os.environ.pop("B1K_EVAL_CONFIG", None)
            else:
                os.environ["B1K_EVAL_CONFIG"] = old_config


class NotifierCardTests(unittest.TestCase):
    def test_all_training_aligned_card_types_are_available(self):
        expected = {
            "start",
            "finished",
            "failed",
            "warning",
            "stalled",
            "heartbeat",
            "watchdog_started",
            "watchdog_stopped",
        }
        self.assertEqual(set(notifier.CARD_TEMPLATES), expected)
        for card_type in expected:
            payload = notifier.build_card(card_type, "标题", "内容", label="B1K评测")
            self.assertEqual(payload["msg_type"], "interactive")
            self.assertIn("[B1K评测]", payload["card"]["header"]["title"]["content"])
        self.assertEqual(notifier.build_card("warning", "标题", "内容")["card"]["header"]["template"], "orange")
        self.assertEqual(notifier.build_card("failed", "标题", "内容")["card"]["header"]["template"], "red")

    def test_unknown_card_type_is_rejected(self):
        with self.assertRaises(ValueError):
            notifier.build_card("unknown", "标题", "内容")

    def test_sign_payload_uses_feishu_hmac_contract(self):
        payload = {"msg_type": "interactive"}
        signed = notifier.sign_payload(payload, "secret", timestamp=123)
        expected = base64.b64encode(
            hmac.new(b"123\nsecret", digestmod=hashlib.sha256).digest()
        ).decode()
        self.assertEqual(signed["timestamp"], "123")
        self.assertEqual(signed["sign"], expected)
        self.assertNotIn("sign", payload)


if __name__ == "__main__":
    unittest.main()
