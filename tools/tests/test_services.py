from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools import service
from tools.config import ROOT, atomic_json, load_env_file
from tools.watchdog import notifier, train
from tools.watchdog.b1k import B1kEvalMonitor


def test_atomic_private_state(tmp_path):
    path = tmp_path / "state.json"
    atomic_json(path, {"value": 1})
    atomic_json(path, {"value": 2})
    assert json.loads(path.read_text()) == {"value": 2}
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(list(tmp_path.iterdir())) == 1


def test_credentials_never_evaluate_shell(tmp_path, monkeypatch):
    secret = tmp_path / "env"
    secret.write_text('export EXAMPLE_VALUE="$(touch sentinel)"\n')
    secret.chmod(0o600)
    monkeypatch.delenv("EXAMPLE_VALUE", raising=False)
    load_env_file(secret)
    assert os.environ["EXAMPLE_VALUE"] == "$(touch sentinel)"
    assert not (tmp_path / "sentinel").exists()
    secret.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        load_env_file(secret)


def test_pid_reuse_and_unrelated_process_are_rejected(tmp_path):
    atomic_json(
        tmp_path / "pid.json",
        {
            "name": "watchdog-b1k",
            "pid": os.getpid(),
            "start_ticks": service.process_start(os.getpid()),
            "root": str(ROOT),
        },
    )
    assert service.running_record(tmp_path, "watchdog-b1k") is None
    with patch.object(service.os, "kill") as kill:
        assert service.stop("watchdog-b1k", tmp_path) == 0
        kill.assert_not_called()


def test_independent_services_start_reuse_stop(tmp_path):
    config = tmp_path / "monitor.json"
    config.write_text(
        json.dumps(
            {
                "notify": False,
                "repo_root": str(tmp_path),
                "log": str(tmp_path / "eval.log"),
                "poll_seconds": 0.05,
            }
        )
    )
    env = {**os.environ, "AGENTS_LOCAL_ROOT": str(tmp_path / "local")}
    for key in ("B1K_EVAL_CONFIG", "B1K_EVAL_LOG"):
        env.pop(key, None)

    def call(kind, *args):
        return subprocess.run(
            [sys.executable, "-m", "tools.watchdog", kind, *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=40,
        )

    try:
        for kind in ("b1k", "train"):
            started = call(kind, "--config", str(config))
            assert started.returncode == 0, started.stderr + started.stdout
        record = json.loads(
            (tmp_path / "local/tools/watchdog-b1k/pid.json").read_text()
        )
        again = call("b1k", "--config", str(config))
        assert again.returncode == 0 and "already running" in again.stdout
        assert str(record["pid"]) in again.stdout
        config.write_text(
            json.dumps({"notify": False, "repo_root": str(tmp_path), "poll_seconds": 2})
        )
        mismatch = call("b1k", "--config", str(config))
        assert mismatch.returncode != 0 and "different configuration" in mismatch.stderr
        assert call("b1k", "--stop").returncode == 0
        state = call("train", "--status", "--json")
        assert json.loads(state.stdout)["running"] is True
        assert call("train", "--stop").returncode == 0
    finally:
        call("b1k", "--stop")
        call("train", "--stop")


def test_b1k_defaults_and_repo_root(tmp_path):
    monitor = B1kEvalMonitor(repo_root=tmp_path, process_checker=lambda: False)
    assert monitor.warning_seconds == 7200
    assert monitor.critical_seconds == 10800
    assert monitor.log_path.is_relative_to(tmp_path)
    with pytest.raises(ValueError):
        B1kEvalMonitor(warning_seconds=10, critical_seconds=1)


def test_invalid_config_cannot_change_kubectl_readonly_command(tmp_path):
    from tools.watchdog.__main__ import factory

    with pytest.raises(ValueError, match="kubectl_args"):
        factory("train")(
            {"notify": False, "kubectl_args": ["delete", "pods"]},
            tmp_path / "config.json",
            tmp_path,
        )
    with pytest.raises(ValueError, match="quiet_hours"):
        factory("b1k")(
            {"notify": False, "quiet_hours": [24]}, tmp_path / "config.json", tmp_path
        )


def test_new_run_resets_stall_baseline(tmp_path):
    path = tmp_path / "eval.log"
    clock = [0.0]
    path.write_text("Heartbeat: completed=100/120 failures=0\n")
    monitor = B1kEvalMonitor(path, process_checker=lambda: True, clock=lambda: clock[0])
    monitor.check_alerts()
    clock[0] = 11000
    path.write_text("Heartbeat: completed=1/120 failures=0\n")
    assert not any(a.key.startswith("eval-stalled") for a in monitor.check_alerts())


def make_notifier(monkeypatch, tmp_path):
    monkeypatch.setattr(
        notifier,
        "legacy_value",
        lambda key: "test-key" if key == "FEISHU_WEBHOOK_KEY" else None,
    )
    return notifier.Notifier({}, tmp_path, "TEST")


def test_feishu_checks_body_and_dedups_only_success(monkeypatch, tmp_path):
    client = make_notifier(monkeypatch, tmp_path)
    response = SimpleNamespace(read=lambda: b'{"code": 19021}')
    with patch.object(notifier.urllib.request, "urlopen") as send:
        send.return_value.__enter__.return_value = response
        assert not client.send_card("failed", "test", "test")
        response.read = lambda: b'{"code": 0}'
        assert client.send_card("failed", "test", "test")
        assert client.send_card("failed", "test", "test")
        assert send.call_count == 2


def test_notifier_exception_never_prints_secret(monkeypatch, tmp_path, capsys):
    client = make_notifier(monkeypatch, tmp_path)
    monkeypatch.setattr(notifier.time, "sleep", lambda _: None)
    with patch.object(
        notifier.urllib.request, "urlopen", side_effect=OSError("SECRET_IN_URL")
    ):
        assert not client.send_card("failed", "test", "test")
    assert "SECRET_IN_URL" not in capsys.readouterr().out


def test_train_dynamic_stall_without_private_patterns(tmp_path, monkeypatch):
    import time

    path = tmp_path / "jobs-dynamic-node0-host.err"
    path.write_text(
        "FSDP-Train: 50%|# | 50/100 [00:10:00<00:10:00, 1.00s/it, loss=0.1, lr=1.00e-05]\n"
    )
    os.utime(path, (time.time() - 1200,) * 2)
    monkeypatch.setattr(train, "KJOB_LOG", tmp_path)
    monkeypatch.setattr(train, "TRAIN_PATTERNS", [])
    result = train.TrainMonitor()._check_stall_events(
        {"job": {"status": "active", "exp_name": "dynamic"}}
    )
    assert len(result) == 1 and result[0][0] == "stalled"


def test_remote_shutdown_kills_only_owned_child(monkeypatch, tmp_path):
    import importlib.util

    monkeypatch.setenv("CLAUDE_FEISHU_STATE", str(tmp_path / "seen.json"))
    monkeypatch.setenv("CLAUDE_FEISHU_SESSIONS", str(tmp_path / "sessions.json"))
    spec = importlib.util.spec_from_file_location(
        "isolated_bridge", ROOT / "tools/claude_feishu/bridge.py"
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        bridge._active_process = child
        bridge.shutdown()
        assert child.wait(timeout=5) == -signal.SIGTERM
        assert bridge._stopping.is_set()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
