"""Independent process lifecycle for workspace tools (Linux, user permissions)."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from typing import Callable

from tools.config import ROOT, atomic_json, load_config, local_root


SERVICES = ("watchdog-b1k", "watchdog-train", "claude-feishu")


@dataclass
class Application:
    run: Callable[[threading.Event], int]
    snapshot: Callable[[], object]
    shutdown: Callable[[], None] | None = None
    ready: bool = True


def process_start(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


def running_record(directory: Path, name: str) -> dict | None:
    try:
        record = json.loads((directory / "pid.json").read_text())
        pid = record["pid"]
        if type(pid) is not int or pid <= 1 or record["name"] != name:
            return None
        if process_start(pid) != record["start_ticks"]:
            return None
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        expected = (
            [b"-m", b"tools.claude_feishu"]
            if name == "claude-feishu"
            else [b"-m", b"tools.watchdog", name.removeprefix("watchdog-").encode()]
        )
        if not any(
            command[i : i + len(expected)] == expected for i in range(len(command))
        ):
            return None
        if b"--foreground" not in command or str(ROOT) != record["root"]:
            return None
        try:
            if Path(f"/proc/{pid}/cwd").resolve() != ROOT:
                return None
        except OSError:
            # 沙箱（如 DSH 的 Landlock）会拒绝读 /proc/<pid>/cwd。读不到 ≠ 进程不在：
            # start_ticks、cmdline、--foreground、root 四道指纹已确认身份，故放行。
            # 若在此返回 None，status 会把活着的服务报成 stopped，stop 更会静默空转
            # （打印 stopped 却不发 SIGTERM），随后的 start 再撞 run.lock 失败。
            pass
        return record
    except (OSError, ValueError, KeyError, TypeError):
        return None


@contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError(
                "another lifecycle operation or instance is running"
            ) from error
        yield


def parser(name: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=f"Agent workspace service: {name}")
    action = result.add_mutually_exclusive_group()
    action.add_argument("--stop", action="store_true")
    action.add_argument("--restart", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--foreground", action="store_true")
    action.add_argument(
        "--once",
        "--check",
        dest="once",
        action="store_true",
        help="validate and inspect once without notifications or starting a daemon",
    )
    result.add_argument("--config", help="machine-local JSON configuration")
    result.add_argument(
        "--no-notify", action="store_true", help="disable watchdog Feishu messages"
    )
    result.add_argument("--json", action="store_true")
    result.add_argument("--restart-timeout", type=int, default=60)
    result.add_argument("--reply-message", help="reply to this Feishu restart command after completion")
    return result


def status(name: str, directory: Path, *, as_json: bool = False) -> int:
    record = running_record(directory, name)
    data = {
        "service": name,
        "running": bool(record),
        "pid": record["pid"] if record else None,
        "ready": bool(record and record.get("ready", False)),
        "restart_supported": bool(record and record.get("generation")),
        "log": str(directory / "service.log"),
    }
    print(
        json.dumps(data, ensure_ascii=False)
        if as_json
        else f"{name}: {'running pid=' + str(data['pid']) if record else 'stopped'} | log={data['log']}"
    )
    return 0


def mark_ready(directory: Path) -> None:
    record = running_record(directory, "claude-feishu")
    if record and record["pid"] == os.getpid():
        record["ready"] = True
        atomic_json(directory / "pid.json", record)


def restart(name: str, directory: Path, *, timeout: int = 60) -> int:
    """Ask the existing service to re-exec; never spawn it from the caller."""
    if not 1 <= timeout <= 300:
        raise ValueError("restart-timeout must be between 1 and 300 seconds")
    with lock(directory / "lifecycle.lock"):
        record = running_record(directory, name)
        if not record or not record.get("generation"):
            raise ValueError("service is stopped or too old for in-process restart; start it once from the host")
        generation = record["generation"]
        # A previous rejected request must not mask this request's validation.
        record.pop("restart_error", None)
        atomic_json(directory / "pid.json", record)
        os.kill(record["pid"], signal.SIGUSR1)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = running_record(directory, name)
            if current:
                if current.get("restart_error"):
                    raise ValueError(current["restart_error"])
                if current.get("generation") != generation and current.get("ready"):
                    print(f"{name}: restarted and ready pid={current['pid']}")
                    return 0
            time.sleep(0.1)
        raise ValueError("restart did not become ready before timeout; inspect service.log")


def stop(name: str, directory: Path) -> int:
    with lock(directory / "lifecycle.lock"):
        record = running_record(directory, name)
        if record is None:
            print(f"{name}: stopped")
            return 0
        os.kill(record["pid"], signal.SIGTERM)
        for _ in range(350):
            if running_record(directory, name) is None:
                print(f"{name}: stopped")
                return 0
            time.sleep(0.1)
        print(
            f"{name}: did not stop; refusing to kill an unverified process",
            file=sys.stderr,
        )
        return 1


def main(
    name: str, command: list[str], factory: Callable, argv: list[str] | None = None
) -> int:
    args = parser(name).parse_args(argv)
    directory = local_root() / "tools" / name
    os.umask(0o077)
    try:
        if args.reply_message and (not args.restart or name != "claude-feishu"):
            raise ValueError("reply-message requires claude-feishu --restart")
        if args.restart:
            error = None
            try:
                restart(name, directory, timeout=args.restart_timeout)
            except (OSError, ValueError) as exc:
                error = str(exc)
            if args.reply_message and error is None:
                # This CLI is spawned by the authenticated native /restart handler.
                # It survives the bridge re-exec and replies only after WS readiness.
                from types import SimpleNamespace

                config, config_path = load_config(name, args.config)
                factory(config, config_path, directory)
                from tools.claude_feishu import bridge

                data = SimpleNamespace(event=SimpleNamespace(message=SimpleNamespace(message_id=args.reply_message)))
                if not bridge._send_reply(data, "✅ bridge 已重启，飞书连接就绪。Claude / DSH 会话已保留。"):
                    print("restart result reply failed", file=sys.stderr)
            if error:
                raise ValueError(error)
            return 0
        if args.status:
            return status(name, directory, as_json=args.json)
        if args.stop:
            return stop(name, directory)
        config, config_path = load_config(name, args.config)
        if args.no_notify:
            config["notify"] = False
        # Environment-selected requests must not silently reuse a different monitor.
        environment = {
            key: os.environ.get(key)
            for key in (
                "B1K_EVAL_LOG",
                "B1K_EVAL_CONFIG",
                "B1K_WATCHDOG_ERROR_PATTERNS",
                "CLAUDE_CLI",
                "CLAUDE_FEISHU_WORKDIR",
            )
        }
        fingerprint = hashlib.sha256(
            json.dumps([config, str(config_path), environment], sort_keys=True).encode()
        ).hexdigest()
        directory.mkdir(parents=True, exist_ok=True)
        if args.once:
            config["notify"] = False
            app = factory(config, config_path, directory)
            print(json.dumps(app.snapshot(), ensure_ascii=False, indent=2))
            return 0
        if not args.foreground:
            with lock(directory / "lifecycle.lock"):
                record = running_record(directory, name)
                if record:
                    if record.get("config_hash") != fingerprint:
                        raise ValueError(
                            "service is running with different configuration; stop it before restarting"
                        )
                    print(f"{name}: already running pid={record['pid']}")
                    return 0
                child_command = [sys.executable, "-m", *command, "--foreground"]
                if args.config:
                    child_command.extend(["--config", str(config_path)])
                if args.no_notify:
                    child_command.append("--no-notify")
                with (directory / "service.log").open("ab", buffering=0) as stream:
                    child = subprocess.Popen(
                        child_command,
                        cwd=ROOT,
                        stdin=subprocess.DEVNULL,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        close_fds=True,
                    )
                for _ in range(150):
                    record = running_record(directory, name)
                    if record and record["pid"] == child.pid:
                        print(
                            f"{name}: started pid={child.pid} | log={directory / 'service.log'}"
                        )
                        return 0
                    if child.poll() is not None:
                        raise ValueError(
                            f"service failed validation/startup; inspect {directory / 'service.log'}"
                        )
                    time.sleep(0.1)
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
                raise ValueError("service startup timed out; child terminated")
        restart_requested = False
        with lock(directory / "run.lock"):
            app = factory(config, config_path, directory)
            event = threading.Event()

            def request_stop(_signum, _frame):
                event.set()
                if app.shutdown:
                    app.shutdown()

            signal.signal(signal.SIGTERM, request_stop)
            signal.signal(signal.SIGINT, request_stop)
            record = {
                "name": name,
                "pid": os.getpid(),
                "start_ticks": process_start(os.getpid()),
                "root": str(ROOT),
                "config_hash": fingerprint,
                "config_path": str(config_path),
                "generation": str(uuid.uuid4()),
                "ready": app.ready,
            }

            def request_restart(_signum, _frame):
                nonlocal restart_requested
                if restart_requested or event.is_set():
                    return
                # Validate updated code/config from the SERVICE's context first.
                # Rejection keeps the old connection alive, including on syntax errors.
                validation = [sys.executable, "-m", *command, "--once", "--config", str(config_path)]
                checked = None
                try:
                    checked = subprocess.run(validation, cwd=ROOT, capture_output=True, timeout=20)
                    valid = checked.returncode == 0
                except (OSError, subprocess.TimeoutExpired):
                    valid = False
                if not valid:
                    print("restart preflight failed; old service kept running", file=sys.stderr)
                    if checked is not None:
                        print(checked.stderr.decode(errors="replace")[-4000:], file=sys.stderr)
                    current = running_record(directory, name) or record
                    current["restart_error"] = "new code/config validation failed; old service kept running"
                    atomic_json(directory / "pid.json", current)
                    return
                restart_requested = True
                request_stop(_signum, _frame)

            signal.signal(signal.SIGUSR1, request_restart)
            atomic_json(directory / "pid.json", record)
            try:
                try:
                    result = app.run(event)
                except SystemExit:
                    if not restart_requested:
                        raise
                    result = 0
            finally:
                (directory / "pid.json").unlink(missing_ok=True)
        if restart_requested:
            # run.lock has been released. Re-exec preserves the original host
            # context, not the requesting agent's inherited Landlock sandbox.
            restart_command = [sys.executable, "-m", *command, "--foreground", "--config", str(config_path)]
            if args.no_notify:
                restart_command.append("--no-notify")
            # Factory-injected CLI/workdir values are derived from config, not
            # caller overrides. Restore the original overrides before reloading.
            for key, value in environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            os.execv(sys.executable, restart_command)
        return result
    except (OSError, ValueError) as error:
        print(f"{name}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--all-status", action="store_true", required=True)
    cli.parse_args()
    for service_name in SERVICES:
        status(service_name, local_root() / "tools" / service_name)
