#!/usr/bin/env python3
"""B1K evaluation log and process monitor.

2026-08-12: eval_status.json has been removed; all progress/result signals
are read from the scheduler log (heartbeat, Job complete, Evaluation finished).
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
import tomllib
from typing import Callable

from tools.config import workspace_root
from tools.watchdog.b1k_layout import (
    checkpoint_layout,
    cloud_run_directory,
    run_directory,
)

REPO_ROOT = workspace_root() / "BEHAVIOR-1K"
DEFAULT_LOGS = (
    REPO_ROOT / "scripts" / "eval" / "fleet" / "eval.log",
    REPO_ROOT / "scripts" / "eval" / "0srv16sim" / "eval.log",
    REPO_ROOT / "scripts" / "eval" / "2srv14sim" / "eval.log",
    REPO_ROOT / "scripts" / "eval" / "8srv8sim" / "eval.log",
)
DEFAULT_PROCESS_MARKERS = (
    "scripts/eval/fleet/eval.py",
    "scripts/eval/0srv16sim/eval.py",
    "scripts/eval/2srv14sim/eval.py",
    "scripts/eval/8srv8sim/eval.py",
)
DEFAULT_PATTERNS = (
    r"out of memory|\bOOM\b",
    r"no space left on device",
    r"Evaluation failed:",
    r"S3 upload failed:",
    r"Traceback \(most recent call last\)",
    r"Policy server \d+ exited",
    r"Segmentation fault",
    r"zenity",
)


@dataclass(frozen=True)
class Alert:
    key: str
    severity: str
    card_type: str
    title: str
    message: str


def eval_process_running() -> bool:
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="ignore")
            )
        except OSError:
            continue
        if (
            any(marker in command for marker in DEFAULT_PROCESS_MARKERS)
            and "--dry-run" not in command
        ):
            return True
    return False


class B1kEvalMonitor:
    name = "b1k-eval"
    alert_interval = 60  # 每分钟检查；相同飞书通知仍由 notifier 去重。
    status_interval = 3600

    def __init__(
        self,
        log_path: Path | None = None,
        process_checker: Callable[[], bool] = eval_process_running,
        clock: Callable[[], float] = time.monotonic,
        *,
        repo_root: Path | None = None,
        config_path: Path | None = None,
        warning_seconds: float | None = None,
        critical_seconds: float | None = None,
    ) -> None:
        self.repo_root = repo_root or REPO_ROOT
        self.config_path = config_path
        self.warning_seconds = warning_seconds
        self.critical_seconds = critical_seconds
        self.default_logs = tuple(
            self.repo_root / "scripts" / "eval" / name / "eval.log"
            for name in ("fleet", "0srv16sim", "2srv14sim", "8srv8sim")
        )
        configured_log = os.environ.get("B1K_EVAL_LOG")
        self.log_override = log_path or (
            Path(configured_log).expanduser() if configured_log else None
        )
        self.log_path = self._select_log()
        is_fleet = self.log_path.parent.name == "fleet" and (
            self.log_override is not None or self.log_path.exists()
        )
        self.warning_seconds = (
            warning_seconds
            if warning_seconds is not None
            else (900 if is_fleet else 7200)
        )
        self.critical_seconds = (
            critical_seconds
            if critical_seconds is not None
            else (3600 if is_fleet else 10800)
        )
        if not 0 < self.warning_seconds <= self.critical_seconds:
            raise ValueError("stall thresholds must satisfy 0 < warning <= critical")
        self.process_checker = process_checker
        self.clock = clock
        self.last_completed: int | None = None
        self.last_progress_at = clock()
        self.last_failures = 0
        self.last_process_running: bool | None = None
        self.log_offset = self._log_size(self.log_path)
        extra_patterns = [
            item.strip()
            for item in os.environ.get("B1K_WATCHDOG_ERROR_PATTERNS", "").split(",")
            if item.strip()
        ]
        self.patterns = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in (*DEFAULT_PATTERNS, *extra_patterns)
        ]

    @staticmethod
    def _log_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _select_log(self) -> Path:
        if self.log_override is not None:
            return Path(self.log_override)
        existing = [path for path in self.default_logs if path.is_file()]
        return (
            max(existing, key=lambda path: path.stat().st_mtime_ns)
            if existing
            else self.default_logs[0]
        )

    def _refresh_log(self) -> Path:
        selected = self._select_log()
        if selected != self.log_path:
            self.log_path = selected
            self.log_offset = 0
        return self.log_path

    def _log_heartbeat(self) -> dict[str, int | str]:
        path = self._refresh_log()
        if not path.is_file():
            return {}
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, 2)
                stream.seek(max(0, size - 256 * 1024))
                lines = stream.read().decode(errors="ignore").splitlines()
        except OSError:
            return {}
        for line in reversed(lines):
            if "Heartbeat:" not in line and "Progress:" not in line:
                continue
            rollout_match = re.search(r"completed=(\d+)/(\d+)", line)
            if rollout_match is None:
                continue
            heartbeat: dict[str, int | str] = {
                "completed_rollouts": int(rollout_match.group(1)),
                "total_rollouts": int(rollout_match.group(2)),
            }
            failures = re.search(r"failures=(\d+)", line)
            if failures:
                heartbeat["failed_rollouts"] = int(failures.group(1))
            eta = re.search(r"ETA=([^,\s]+)", line)
            if eta:
                heartbeat["eta"] = eta.group(1)
            return heartbeat
        return {}

    def _log_results(self) -> dict[str, int | float]:
        """Parse the latest 'Current results: ...' line from the scheduler log.

        The scheduler prints one cumulative line after each completed job
        (aggregating summary.json from all complete jobs on disk), so the value
        stays correct across log rotation/resume.
        """
        path = self._refresh_log()
        if not path.is_file():
            return {}
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return {}
        for line in reversed(lines):
            if "Current results:" not in line:
                continue
            result: dict[str, int | float] = {}
            rollouts = re.search(r"rollouts=(\d+)", line)
            sr = re.search(r"SR=(\d+)/(\d+)", line)
            if rollouts:
                result["completed_rollouts"] = int(rollouts.group(1))
            if sr:
                result.update(
                    successes=int(sr.group(1)), result_rollouts=int(sr.group(2))
                )
            q = re.search(r"Q=([\d.]+|-)", line)
            if q and q.group(1) != "-":
                result["mean_q"] = float(q.group(1))
            return result
        return {}

    def _eval_toml_path(self) -> Path:
        if self.config_path is not None:
            return self.config_path
        configured = os.environ.get("B1K_EVAL_CONFIG")
        if configured:
            return Path(configured).expanduser()
        return self.repo_root / "scripts" / "eval" / "eval.toml"

    def _per_checkpoint_results(self) -> list[str]:
        """Aggregate exact summaries selected by the current eval TOML.

        Resolving each job through the shared artifact-layout helper keeps
        resume results while excluding other repeat namespaces and h/n runs.
        """
        toml_path = self._eval_toml_path()
        try:
            payload = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return []
        defaults = payload.get("defaults", {})
        raw_jobs = payload.get("jobs", [])
        if not isinstance(defaults, dict) or not isinstance(raw_jobs, list):
            return []

        groups: dict[tuple[object, ...], dict[str, float | str]] = {}
        seen_summaries: set[Path] = set()
        for raw_job in raw_jobs:
            if not isinstance(raw_job, dict):
                continue
            job = {**defaults, **raw_job}
            try:
                task = str(job["task"])
                action_horizon = int(job.get("action_horizon", 16))
                output_root = Path(str(job["output_root"])).expanduser()
                if not output_root.is_absolute():
                    output_root = (toml_path.parent / output_root).resolve()
                is_cloud = job.get("model_type") == "cloud" or "release_config" in job
                if is_cloud:
                    release_path = Path(str(job["release_config"])).expanduser()
                    if not release_path.is_absolute():
                        release_path = (toml_path.parent / release_path).resolve()
                    release = json.loads(release_path.read_text(encoding="utf-8"))
                    model_id = str(release["model_id"])
                    model_digest = str(release["model_digest"])
                    summary_path = (
                        cloud_run_directory(
                            output_root,
                            model_id,
                            model_digest,
                            task,
                            action_horizon=action_horizon,
                        )
                        / "summary.json"
                    )
                    key = ("cloud", model_id, model_digest, action_horizon)
                    label = str(job.get("checkpoint_id") or model_id).strip()
                    if action_horizon != 16:
                        label += f"/h{action_horizon}"
                else:
                    checkpoint = Path(str(job["checkpoint"])).expanduser()
                    if not checkpoint.is_absolute():
                        checkpoint = toml_path.parent / checkpoint
                    checkpoint = checkpoint.resolve()
                    num_steps = int(job.get("num_steps", 10))
                    namespace = job.get("run_namespace")
                    summary_path = (
                        run_directory(
                            output_root,
                            checkpoint,
                            task,
                            action_horizon=action_horizon,
                            num_steps=num_steps,
                            namespace=namespace,
                        )
                        / "summary.json"
                    )
                    key = (
                        "checkpoint",
                        str(checkpoint),
                        str(namespace or ""),
                        action_horizon,
                        num_steps,
                    )
                    layout = checkpoint_layout(checkpoint)
                    label = str(job.get("checkpoint_id") or "").strip()
                    if not label:
                        label = f"{layout.experiment}/{layout.point}/{layout.weight}"
                    if namespace:
                        label += f"/{namespace}"
                    if action_horizon != 16 or num_steps != 10:
                        label += f"/h{action_horizon}-n{num_steps}"
            except (KeyError, OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
            if summary_path in seen_summaries:
                continue
            seen_summaries.add(summary_path)
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            done = int(summary.get("completed_rollouts") or 0)
            if done == 0:
                continue

            group = groups.setdefault(
                key,
                {"label": label, "sr": 0.0, "tot": 0.0, "q": 0.0},
            )
            group["sr"] = float(group["sr"]) + int(summary.get("successes") or 0)
            group["tot"] = float(group["tot"]) + done
            group["q"] = (
                float(group["q"]) + float(summary.get("mean_q_score") or 0.0) * done
            )

        lines = []
        for group in sorted(groups.values(), key=lambda item: str(item["label"])):
            total = int(group["tot"])
            mean_q = float(group["q"]) / total if total else 0.0
            lines.append(
                f"{group['label']}: SR {int(group['sr'])}/{total} Q {mean_q:.3f}"
            )
        return lines

    def _log_final_status(self) -> str | None:
        """Return the last 'Evaluation finished: status=...' value, if any."""
        path = self._refresh_log()
        if not path.is_file():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        matches = re.findall(r"Evaluation finished: status=(\S+)", text)
        return matches[-1] if matches else None

    def _new_log_alerts(self) -> list[Alert]:
        path = self._refresh_log()
        if not path.is_file():
            return []
        try:
            size = path.stat().st_size
            if size < self.log_offset:
                self.log_offset = 0
            with path.open(encoding="utf-8", errors="ignore") as stream:
                stream.seek(self.log_offset)
                content = stream.read()
                self.log_offset = stream.tell()
        except OSError:
            return []
        lines = [
            line.strip()
            for line in content.splitlines()
            if any(pattern.search(line) for pattern in self.patterns)
        ]
        if not lines:
            return []
        excerpt = "\n  ".join(lines[-5:])
        return [
            Alert(
                "eval-log-error",
                "critical",
                "failed",
                "评测日志异常",
                f"{self.card_content('异常')}\n- 原因：{excerpt}",
            )
        ]

    def check_alerts(self) -> list[Alert]:
        heartbeat = self._log_heartbeat()
        completed = int(heartbeat.get("completed_rollouts", 0) or 0)
        failures = int(heartbeat.get("failed_rollouts", 0) or 0)
        running = self.process_checker()
        now = self.clock()
        alerts = self._new_log_alerts()

        if (
            self.last_completed is None
            or completed != self.last_completed
            or self.last_process_running is False
        ):
            self.last_progress_at = now
        elif running:
            stalled = now - self.last_progress_at
            if stalled >= self.critical_seconds:
                alerts.append(
                    Alert(
                        "eval-stalled-critical",
                        "critical",
                        "stalled",
                        f"评测停滞超过 {self.critical_seconds / 60:g} 分钟",
                        f"{self.card_content('停滞')}\n- 原因：超过严重停滞阈值，请检查 sim/GPU/log",
                    )
                )
            elif stalled >= self.warning_seconds:
                alerts.append(
                    Alert(
                        "eval-stalled-warning",
                        "warning",
                        "stalled",
                        f"评测停滞超过 {self.warning_seconds / 60:g} 分钟",
                        f"{self.card_content('停滞')}\n- 原因：超过停滞提醒阈值，当前进程仍在",
                    )
                )
        if failures > self.last_failures:
            alerts.append(
                Alert(
                    "eval-failures-increased",
                    "critical",
                    "failed",
                    "评测失败数增加",
                    f"{self.card_content('存在失败')}\n- 原因：failed_rollouts {self.last_failures} → {failures}",
                )
            )

        previous_running = self.last_process_running
        # 首次检查（last_process_running=None）不触发 started 告警：
        # watchdog 启动时进程可能已在跑（或日志尚未更新），此时发卡片会
        # 带上旧 request 的残留数据
        if running and previous_running is not True and previous_running is not None:
            alerts.append(
                Alert(
                    "eval-started",
                    "info",
                    "start",
                    "评测已启动",
                    self._started_content(),
                )
            )
        elif previous_running is True and not running:
            final = self._log_final_status()
            if final in {"complete", "partial"}:
                state = (
                    "已完成"
                    if final == "complete"
                    else "阶段完成（request 未全部完成）"
                )
                alerts.append(
                    Alert(
                        "eval-finished",
                        "info",
                        "finished",
                        "评测已结束",
                        self.card_content(state),
                    )
                )
            else:
                alerts.append(
                    Alert(
                        "eval-process-died",
                        "critical",
                        "failed",
                        "评测进程异常退出",
                        self.card_content(f"异常结束（status={final or 'unknown'}）"),
                    )
                )
        self.last_completed = completed
        self.last_failures = failures
        self.last_process_running = running
        return alerts

    def active(self) -> bool:
        return self.process_checker()

    def _toml_request_id(self) -> str:
        """从当前 eval.toml 的 [request] 表读 request_id（B1K 日志无 request 行）。"""
        toml_path = self._eval_toml_path()
        try:
            text = toml_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
        match = re.search(r"request_id\s*=\s*\"([^\"]+)\"", text)
        return match.group(1) if match else ""

    def _started_content(self) -> str:
        """评测启动卡片（与训练侧同格式：第一行加粗评测名 - 状态 + bullets）。
        数据用实时日志心跳与 eval.toml 的 request_id。"""
        request_id = self._toml_request_id() or "B1K Eval"
        heartbeat = self._log_heartbeat()
        completed = heartbeat.get("completed_rollouts")
        lines = [f"**{request_id}** - 运行中"]
        if completed is None:
            lines.append("- 状态待发布（启动早期）")
        else:
            lines.append(
                f"- 进度：{completed}/{heartbeat.get('total_rollouts', '?')} rollouts，"
                f"failures {heartbeat.get('failed_rollouts', 0)}"
            )
        lines.append(f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
        return "\n".join(lines)

    def card_content(self, state: str) -> str:
        heartbeat = self._log_heartbeat()
        per_checkpoint = self._per_checkpoint_results()
        results_text = (
            "- 分组结果：\n  " + "\n  ".join(per_checkpoint[:10])
            if per_checkpoint
            else "- 结果：暂无"
        )
        return "\n".join(
            (
                f"**{self._toml_request_id() or 'B1K Eval'}** - {state}",
                f"- 进度：{heartbeat.get('completed_rollouts', 0)}/{heartbeat.get('total_rollouts', '?')} rollouts",
                results_text,
                f"- 失败：{heartbeat.get('failed_rollouts', 0)}",
                f"- ETA：{heartbeat.get('eta', '-')}",
                f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            )
        )

    def status(self) -> str:
        heartbeat = self._log_heartbeat()
        result = self._log_results()
        completed = int(result.get("completed_rollouts", 0) or 0)
        successes = int(result.get("successes", 0) or 0)
        mean_q = result.get("mean_q")
        q_text = f"{mean_q:.3f}" if mean_q is not None else "-"
        return (
            f"进度 {heartbeat.get('completed_rollouts', 0)}/{heartbeat.get('total_rollouts', '?')} rollouts；"
            f"结果 SR {successes}/{completed}，Q {q_text}；"
            f"失败 {heartbeat.get('failed_rollouts', 0)}，ETA {heartbeat.get('eta', '-')}"
        )
