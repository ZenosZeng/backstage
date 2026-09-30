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


def test_sandbox_unreadable_cwd_keeps_a_live_process_visible(tmp_path, monkeypatch):
    """回归：沙箱拒绝读 /proc/<pid>/cwd 时，不能把活着的服务判成已停止。

    DSH 的 Landlock 沙箱下跑 scripts/restart-claude-feishu.sh 实测复现：cwd 读被拒
    → running_record 返回 None → status 误报未运行、stop 静默空转（打印 stopped 却不发
    SIGTERM）→ 随后的 start 撞上仍在运行的 run.lock，重启失败。其余四道指纹已足以
    确认身份，故 cwd 读不到时应放行。
    """
    atomic_json(
        tmp_path / "pid.json",
        {
            "name": "claude-feishu",
            "pid": os.getpid(),
            "start_ticks": service.process_start(os.getpid()),
            "root": str(ROOT),
        },
    )
    real_path = service.Path

    class _SandboxedProcPath:
        """只让 /proc 下的 cwd 解析失败，其余路径行为不变。"""

        def __init__(self, path):
            self._real = real_path(path)

        def read_bytes(self):
            return b"-m\0tools.claude_feishu\0--foreground\0"

        def resolve(self):
            raise PermissionError(13, "Permission denied")

        def __getattr__(self, name):
            return getattr(self._real, name)

    monkeypatch.setattr(service, "Path", _SandboxedProcPath)
    assert service.running_record(tmp_path, "claude-feishu") is not None


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


def test_restart_rejects_legacy_service_before_signalling(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "running_record", lambda *args: {"pid": 123})
    with patch.object(service.os, "kill") as kill:
        with pytest.raises(ValueError, match="too old"):
            service.restart("claude-feishu", tmp_path)
        kill.assert_not_called()


def test_reexec_restores_caller_overrides_and_releases_run_lock(monkeypatch, tmp_path):
    config = tmp_path / "config.json"
    config.write_text("{}")
    monkeypatch.setenv("AGENTS_LOCAL_ROOT", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CLI", raising=False)
    monkeypatch.delenv("CLAUDE_FEISHU_WORKDIR", raising=False)
    handlers = {}
    monkeypatch.setattr(service.signal, "signal", lambda signum, handler: handlers.update({signum: handler}))
    monkeypatch.setattr(service.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))

    def factory(*args):
        os.environ["CLAUDE_CLI"] = "injected-by-factory"
        os.environ["CLAUDE_FEISHU_WORKDIR"] = str(tmp_path)

        def run(stop):
            handlers[signal.SIGUSR1](signal.SIGUSR1, None)
            assert stop.is_set()
            return 0

        return service.Application(run=run, snapshot=lambda: {})

    def execute(binary, argv):
        assert "CLAUDE_CLI" not in os.environ
        assert "CLAUDE_FEISHU_WORKDIR" not in os.environ
        assert argv[-2:] == ["--config", str(config)]
        with service.lock(tmp_path / "claude-feishu/run.lock"):
            pass
        raise RuntimeError("re-exec boundary reached")

    monkeypatch.setattr(service.os, "execv", execute)
    with pytest.raises(RuntimeError, match="re-exec boundary reached"):
        service.main("claude-feishu", ["tools.claude_feishu"], factory,
                     ["--foreground", "--config", str(config)])


def test_live_restart_keeps_pid_and_rejects_invalid_config(tmp_path):
    config = tmp_path / "monitor.json"
    valid = {"notify": False, "repo_root": str(tmp_path), "poll_seconds": 0.05}
    config.write_text(json.dumps(valid))
    env = {**os.environ, "AGENTS_LOCAL_ROOT": str(tmp_path / "local")}
    for key in ("B1K_EVAL_CONFIG", "B1K_EVAL_LOG"):
        env.pop(key, None)

    def call(*args):
        return subprocess.run([sys.executable, "-m", "tools.watchdog", "b1k", *args],
                              cwd=ROOT, env=env, text=True, capture_output=True, timeout=40)

    record_path = tmp_path / "local/tools/watchdog-b1k/pid.json"
    try:
        assert call("--config", str(config)).returncode == 0
        before = json.loads(record_path.read_text())
        completed = call("--restart", "--restart-timeout", "15")
        assert completed.returncode == 0, completed.stderr
        after = json.loads(record_path.read_text())
        assert after["pid"] == before["pid"]
        assert after["start_ticks"] == before["start_ticks"]
        assert after["generation"] != before["generation"]
        assert after["config_path"] == str(config)
        assert after["config_hash"] == before["config_hash"]
        config.write_text(json.dumps({**valid, "poll_seconds": -1}))
        rejected = call("--restart", "--restart-timeout", "15")
        assert rejected.returncode != 0 and "old service kept running" in rejected.stderr
        unchanged = json.loads(record_path.read_text())
        assert unchanged["generation"] == after["generation"]
        assert unchanged["ready"] is True
        assert json.loads(call("--status", "--json").stdout)["running"] is True
        config.write_text(json.dumps(valid))
        assert call("--restart", "--restart-timeout", "15").returncode == 0
    finally:
        call("--stop")


def test_landlocked_caller_requests_host_restart_without_inheriting_sandbox(tmp_path):
    """Real kernel restriction: the caller cannot write host storage before OR after restart."""
    import ctypes
    import platform

    if platform.machine() != "x86_64":
        pytest.skip("Landlock syscall numbers in this regression target Linux x86_64")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.syscall(444, 0, 0, 1) < 1:
        pytest.skip("kernel does not expose Landlock")
    local = tmp_path / "local"
    config = tmp_path / "monitor.json"
    config.write_text(json.dumps({"notify": False, "repo_root": str(tmp_path), "poll_seconds": 0.05}))
    protected = tmp_path / "host-storage"
    protected.mkdir()
    env = {**os.environ, "AGENTS_LOCAL_ROOT": str(local)}
    for key in ("B1K_EVAL_CONFIG", "B1K_EVAL_LOG"):
        env.pop(key, None)
    command = [sys.executable, "-m", "tools.watchdog", "b1k"]

    def call(*args):
        return subprocess.run([*command, *args], cwd=ROOT, env=env,
                              text=True, capture_output=True, timeout=40)

    script = '''
import ctypes,errno,os,subprocess,sys
from pathlib import Path
libc=ctypes.CDLL(None,use_errno=True)
abi=libc.syscall(444,0,0,1)
rights=(1<<1)|sum(1<<i for i in range(4,13))
if abi>=2:rights|=1<<13
if abi>=3:rights|=1<<14
class Ruleset(ctypes.Structure):_fields_=[('handled_access_fs',ctypes.c_uint64)]
class PathRule(ctypes.Structure):
 _pack_=1
 _fields_=[('allowed_access',ctypes.c_uint64),('parent_fd',ctypes.c_int32)]
attr=Ruleset(rights)
fd=libc.syscall(444,ctypes.byref(attr),ctypes.sizeof(attr),0)
assert fd>=0,ctypes.get_errno()
directory=os.open(sys.argv[1],os.O_PATH)
rule=PathRule(rights,directory)
assert libc.syscall(445,fd,1,ctypes.byref(rule),0)==0,ctypes.get_errno()
assert libc.prctl(38,1,0,0,0)==0
assert libc.syscall(446,fd,0)==0,ctypes.get_errno()
os.close(directory);os.close(fd)
def denied():
 try:Path(sys.argv[2]).write_text('must not write')
 except OSError as e:assert e.errno==errno.EACCES
 else:raise AssertionError('caller unexpectedly gained host write permission')
denied()
result=subprocess.run([sys.executable,'-m','tools.watchdog','b1k','--restart','--restart-timeout','15'],
                      text=True,capture_output=True)
assert result.returncode==0,result.stderr
denied()
print('DENIED before/after; host restart ready')
'''
    try:
        assert call("--config", str(config)).returncode == 0
        record_path = local / "tools/watchdog-b1k/pid.json"
        before = json.loads(record_path.read_text())
        requested = subprocess.run([sys.executable, "-c", script, str(local), str(protected / "denied")],
                                   cwd=ROOT, env=env, capture_output=True, text=True, timeout=40)
        assert requested.returncode == 0, requested.stderr
        assert "DENIED before/after" in requested.stdout
        after = json.loads(record_path.read_text())
        assert after["generation"] != before["generation"]
        assert after["pid"] == before["pid"]
        assert after["ready"] is True
        assert not (protected / "denied").exists()
    finally:
        call("--stop")
