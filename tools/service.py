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
from typing import Callable

from tools.config import ROOT, atomic_json, load_config, local_root


SERVICES = ("watchdog-b1k", "watchdog-train", "claude-feishu")


@dataclass
class Application:
    run: Callable[[threading.Event], int]
    snapshot: Callable[[], object]
    shutdown: Callable[[], None] | None = None


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
        if Path(f"/proc/{pid}/cwd").resolve() != ROOT:
            return None
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
    return result


def status(name: str, directory: Path, *, as_json: bool = False) -> int:
    record = running_record(directory, name)
    data = {
        "service": name,
        "running": bool(record),
        "pid": record["pid"] if record else None,
        "log": str(directory / "service.log"),
    }
    print(
        json.dumps(data, ensure_ascii=False)
        if as_json
        else f"{name}: {'running pid=' + str(data['pid']) if record else 'stopped'} | log={data['log']}"
    )
    return 0


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
            }
            atomic_json(directory / "pid.json", record)
            try:
                return app.run(event)
            finally:
                (directory / "pid.json").unlink(missing_ok=True)
    except (OSError, ValueError) as error:
        print(f"{name}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--all-status", action="store_true", required=True)
    cli.parse_args()
    for service_name in SERVICES:
        status(service_name, local_root() / "tools" / service_name)
