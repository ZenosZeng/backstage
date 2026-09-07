"""Regression tests for the standalone training watchdog."""

from __future__ import annotations

import os
import json
import time
import pathlib
import datetime as dt

import pytest

from tools.watchdog import notifier as watchdog_notifier
from tools.watchdog import runner as watchdog_scheduler
from tools.watchdog import train as watchdog_train


def _write_progress_log(path: pathlib.Path, *, age_minutes: float) -> None:
    path.write_text(
        "Host=node001 world=8 local_rank=0 batch-size=192 epochs=4 steps_per_epoch=100\n"
        "FSDP-Train:  50%|#####     | 50/100 [00:10:00<00:10:00, "
        "1.00s/it, loss=0.1000, lr=1.00e-05]\n"
    )
    modified = time.time() - age_minutes * 60
    os.utime(path, (modified, modified))


def _write_offline_status(
    root: pathlib.Path,
    *,
    exp_name: str,
    now: float,
    activity_age_minutes: float,
) -> pathlib.Path:
    registry = root / watchdog_train.OFFLINE_STATUS_DIR
    registry.mkdir()
    child_log = root / "eval-job-running.log"
    child_log.write_text("[episode 1] chunk 2/10\n")
    activity_time = now - activity_age_minutes * 60
    os.utime(child_log, (activity_time, activity_time))
    started = dt.datetime.fromtimestamp(now - 7200, tz=dt.timezone.utc).isoformat()
    payload = {
        "schema": watchdog_train.OFFLINE_STATUS_SCHEMA,
        "request_id": "golden-smoke",
        "plan": {"max_workers": 4},
        "status": "running",
        "started_at": started,
        "finished_at": None,
        "counts": {
            "total": 4,
            "pending": 2,
            "running": 1,
            "complete": 1,
            "skipped": 0,
            "failed": 0,
        },
        "jobs": {
            "done": {"status": "complete", "elapsed_seconds": 3600, "log": ""},
            "running": {
                "status": "running",
                "elapsed_seconds": None,
                "log": str(child_log),
            },
            "pending-a": {"status": "pending", "elapsed_seconds": None, "log": ""},
            "pending-b": {"status": "pending", "elapsed_seconds": None, "log": ""},
        },
    }
    status_path = registry / f"{exp_name}.json"
    status_path.write_text(json.dumps(payload))
    os.utime(status_path, (activity_time, activity_time))
    return status_path


def test_stall_alerts_only_cover_active_jobs_and_do_not_oscillate(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(watchdog_train, "KJOB_LOG", tmp_path)
    monkeypatch.setattr(
        watchdog_train, "TRAIN_PATTERNS", [("experiment", "*experiment*")]
    )
    log_path = tmp_path / "jobs-experiment-node001.err"
    active_jobs = {"job": {"status": "active", "exp_name": "experiment"}}
    monitor = watchdog_train.TrainMonitor()

    _write_progress_log(log_path, age_minutes=20)
    warning = monitor._check_stall_events(active_jobs)
    assert [event[0] for event in warning] == ["stalled"]
    assert monitor._check_stall_events(active_jobs) == []

    _write_progress_log(log_path, age_minutes=61)
    critical = monitor._check_stall_events(active_jobs)
    assert [event[0] for event in critical] == ["failed"]
    assert monitor._check_stall_events(active_jobs) == []

    inactive_jobs = {"job": {"status": "succeeded", "exp_name": "experiment"}}
    assert monitor._check_stall_events(inactive_jobs) == []
    assert "experiment" not in monitor._stalled_reported


def test_offline_eval_progress_and_card_use_shared_format(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = time.time()
    exp_name = "offline-eval-12345"
    monkeypatch.setattr(watchdog_train, "KJOB_LOG", tmp_path)
    _write_offline_status(
        tmp_path,
        exp_name=exp_name,
        now=now,
        activity_age_minutes=1,
    )

    progress = watchdog_train._offline_progress(exp_name, now=now)
    body = watchdog_train._event_body("kjob-1", exp_name, "运行中", progress)

    assert progress is not None
    assert progress["step"] == 1
    assert progress["remaining_h"] == pytest.approx(0.75)
    assert progress["activity_age_min"] == pytest.approx(1.0)
    assert body.startswith(f"**{exp_name}** - 运行中（评测）")
    assert "- 请求: golden-smoke" in body
    assert "- 进度 job: 1/4 (25.0%)" in body
    assert "- 运行中: 1 / 失败: 0" in body
    assert "- Loss:" not in body


def test_offline_eval_stall_uses_child_log_activity(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = time.time()
    exp_name = "offline-eval-12345"
    monkeypatch.setattr(watchdog_train, "KJOB_LOG", tmp_path)
    monkeypatch.setattr(watchdog_train.time, "time", lambda: now)
    _write_offline_status(
        tmp_path,
        exp_name=exp_name,
        now=now,
        activity_age_minutes=20,
    )
    monitor = watchdog_train.TrainMonitor()
    active_jobs = {"kjob-1": {"status": "active", "exp_name": exp_name}}

    warning = monitor._check_stall_events(active_jobs)

    assert [event[0] for event in warning] == ["stalled"]
    assert warning[0][3]["type"] == "offline_eval"
    assert monitor._check_stall_events(active_jobs) == []


def test_kubectl_job_maps_to_offline_eval_log(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = "2026-08-10T07:00:00Z"
    created_epoch = dt.datetime(2026, 8, 10, 7, tzinfo=dt.timezone.utc).timestamp()
    log_path = tmp_path / "offline-eval-12345.out"
    log_path.write_text("started\n")
    os.utime(log_path, (created_epoch, created_epoch))
    payload = {
        "items": [
            {
                "metadata": {"name": "slurm-profile-abc", "creationTimestamp": created},
                "status": {"active": 1},
            }
        ]
    }
    monkeypatch.setattr(watchdog_train, "KJOB_LOG", tmp_path)
    monkeypatch.setattr(watchdog_train, "_run", lambda _cmd: json.dumps(payload))

    jobs = watchdog_train._kubectl_jobs()

    assert jobs["slurm-profile-abc"]["exp_name"] == "offline-eval-12345"


def test_dynamic_progress_resolves_single_node_log(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exp_name = "0813_goai_combo"
    log_path = tmp_path / f"jobs-1-node031-{exp_name}.err"
    _write_progress_log(log_path, age_minutes=1)
    monkeypatch.setattr(watchdog_train, "KJOB_LOG", tmp_path)
    monkeypatch.setattr(watchdog_train, "TRAIN_PATTERNS", [])

    progress = watchdog_train.TrainMonitor()._progress_of(exp_name)

    assert progress is not None
    assert progress["step"] == 50
    assert watchdog_train._latest_train_err(exp_name) == log_path


def test_dynamic_progress_resolves_multi_node_rank_zero_log(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exp_name = "0811_devices12"
    rank_zero_log = tmp_path / f"jobs-{exp_name}-node0-node063.err"
    rank_one_log = tmp_path / f"jobs-{exp_name}-node1-node068.err"
    _write_progress_log(rank_zero_log, age_minutes=2)
    rank_one_log.write_text("rank 1 has no main tqdm progress\n")
    modified = time.time() - 60
    os.utime(rank_one_log, (modified, modified))
    monkeypatch.setattr(watchdog_train, "KJOB_LOG", tmp_path)
    monkeypatch.setattr(watchdog_train, "TRAIN_PATTERNS", [])

    progress = watchdog_train.TrainMonitor()._progress_of(exp_name)

    assert progress is not None
    assert progress["step"] == 50
    assert watchdog_train._latest_train_err(exp_name) == rank_zero_log


@pytest.mark.parametrize(
    ("sent", "message"),
    [(True, "event sent"), (False, "event send failed")],
)
def test_emit_reports_notifier_result(
    sent: bool,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    watchdog_train.TrainMonitor(sender=lambda *_args: sent)._emit(
        "start", "job-1", "experiment", None, []
    )
    assert message in capsys.readouterr().out


def test_status_numbers_only_active_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        watchdog_train,
        "_kubectl_jobs",
        lambda: {
            "job-a": {"status": "succeeded", "exp_name": "finished"},
            "job-b": {"status": "active", "exp_name": "running-b"},
            "job-c": {"status": "active", "exp_name": "running-c"},
        },
    )
    monitor = watchdog_train.TrainMonitor()
    monkeypatch.setattr(monitor, "_progress_of", lambda _exp: None)

    status = monitor.status()

    assert "**job1: running-b**" in status
    assert "**job2: running-c**" in status
    assert "job3" not in status
    assert "finished" not in status


def test_card_colors_are_distinct_and_semantic() -> None:
    templates = watchdog_notifier.CARD_TEMPLATES
    assert templates["start"][0] == "blue"
    assert templates["finished"][0] == "green"
    assert templates["failed"][0] == "red"
    assert templates["heartbeat"][0] == "turquoise"
    assert templates["watchdog_started"][0] == "violet"
    assert templates["watchdog_stopped"][0] == "yellow"


def test_event_body_uses_mobile_friendly_bullets() -> None:
    body = watchdog_train._event_body(
        "job-1",
        "experiment",
        "完成",
        {
            "epoch": (4.0, 4),
            "step": 100,
            "total": 100,
            "elapsed_h": 1.0,
            "its": 1.0,
            "loss": "0.0123",
        },
        [("备注", "格式预览")],
    )

    assert body.startswith("**experiment** - 完成（训练）")
    assert "| 字段 |" not in body
    assert "- kjob: job-1" in body
    assert "- 进度 step: 100/100 (100.0%)" in body
    assert "- 备注: 格式预览" in body


def test_lifecycle_body_uses_mobile_friendly_bullets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monitor = type("TestMonitor", (), {"name": "train"})()
    monkeypatch.setattr(watchdog_scheduler.socket, "gethostname", lambda: "node043")
    monkeypatch.setattr(watchdog_scheduler.os, "getpid", lambda: 1234)

    body = watchdog_scheduler.lifecycle("已停止", monitor.name)

    assert body.startswith("**Watchdog** - 已停止")
    assert "| 字段 |" not in body
    assert "- 主机: node043" in body
    assert "- PID: 1234" in body
    assert "- Monitor: train" in body


def test_main_sends_start_and_stop_lifecycle_cards() -> None:
    import threading

    sent = []
    stop = threading.Event()
    stop.set()
    notifier = type(
        "Notifier",
        (),
        {"send_card": lambda self, kind, *args: sent.append(kind) or True},
    )()
    monitor = type("Monitor", (), {"name": "train"})()
    assert watchdog_scheduler.run(monitor, notifier, stop) == 0
    assert sent == ["watchdog_started", "watchdog_stopped"]
