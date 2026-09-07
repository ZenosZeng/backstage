from __future__ import annotations

import argparse
from pathlib import Path
import sys

from tools.config import resolve_path, workspace_root
from tools.service import Application, main
from tools.watchdog.notifier import Notifier
from tools.watchdog.runner import run


def factory(kind: str):
    def build(config: dict, config_path: Path, directory: Path) -> Application:
        quiet = config.get("quiet_hours")
        if quiet is not None and (
            not isinstance(quiet, list)
            or any(type(h) is not int or not 0 <= h <= 23 for h in quiet)
        ):
            raise ValueError("quiet_hours must be a list of hours from 0 to 23")
        notifier = Notifier(
            config, config_path.parent, "B1K评测" if kind == "b1k" else "Pi训练"
        )
        if kind == "b1k":
            from tools.watchdog.b1k import B1kEvalMonitor

            repo = resolve_path(
                config.get("repo_root", workspace_root() / "BEHAVIOR-1K"),
                config_path.parent,
            )
            monitor = B1kEvalMonitor(
                log_path=resolve_path(config["log"], config_path.parent)
                if config.get("log")
                else None,
                repo_root=repo,
                config_path=resolve_path(config["eval_config"], config_path.parent)
                if config.get("eval_config")
                else None,
                warning_seconds=float(config["stall_warning_seconds"])
                if "stall_warning_seconds" in config
                else None,
                critical_seconds=float(config["stall_critical_seconds"])
                if "stall_critical_seconds" in config
                else None,
            )
        else:
            from tools.watchdog import train

            default_repo = workspace_root() / "Pi_b1k"
            if not default_repo.is_dir():
                default_repo = workspace_root() / "Pi"
            repo = resolve_path(
                config.get("repo_root", default_repo), config_path.parent
            )
            train.KJOB_LOG = resolve_path(
                config.get("log_root", repo / "kjob_logs"), config_path.parent
            )
            train.STATE_FILE = directory / "cursors.json"
            experiments = config.get("experiments", [])
            if not isinstance(experiments, list) or any(
                not isinstance(item, list)
                or len(item) != 2
                or any(not isinstance(value, str) for value in item)
                for item in experiments
            ):
                raise ValueError("experiments must contain [label, glob] pairs")
            train.TRAIN_PATTERNS = [tuple(item) for item in experiments]
            flags = config.get("kubectl_args", [])
            allowed = {
                "--context",
                "--namespace",
                "-n",
                "--kubeconfig",
                "--cluster",
                "--request-timeout",
            }
            if (
                not isinstance(flags, list)
                or len(flags) % 2
                or any(not isinstance(x, str) for x in flags)
                or any(
                    flags[i] not in allowed
                    or not flags[i + 1]
                    or flags[i + 1].startswith("-")
                    for i in range(0, len(flags), 2)
                )
            ):
                raise ValueError(
                    "kubectl_args accepts only context/namespace/cluster/kubeconfig/request-timeout option-value pairs"
                )
            train.KUBECTL_ARGS = flags
            train.STALL_WARNING_MIN = (
                float(config.get("stall_warning_seconds", 900)) / 60
            )
            train.STALL_FAIL_MIN = (
                float(config.get("stall_critical_seconds", 3600)) / 60
            )
            if not 0 < train.STALL_WARNING_MIN <= train.STALL_FAIL_MIN:
                raise ValueError(
                    "stall thresholds must satisfy 0 < warning <= critical"
                )
            monitor = train.TrainMonitor(
                sender=lambda kind, body: notifier.send_card(kind, "任务状态", body)
            )
        poll = float(config.get("poll_seconds", 60))
        if poll <= 0:
            raise ValueError("poll_seconds must be positive")
        return Application(
            run=lambda stop: run(
                monitor,
                notifier,
                stop,
                poll_seconds=poll,
                quiet_hours=config.get("quiet_hours"),
            ),
            snapshot=lambda: {
                "monitor": monitor.name,
                "repo_root": str(repo),
                "status": monitor.status(),
            },
        )

    return build


def entry() -> int:
    cli = argparse.ArgumentParser(add_help=False)
    cli.add_argument("kind", choices=("b1k", "train"))
    selected, rest = cli.parse_known_args()
    return main(
        f"watchdog-{selected.kind}",
        ["tools.watchdog", selected.kind],
        factory(selected.kind),
        rest,
    )


if __name__ == "__main__":
    sys.exit(entry())
