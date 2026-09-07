#!/usr/bin/env python3
"""TrainMonitor: event-driven training and offline-evaluation watchdog.

Emits four event kinds to Feishu as mobile-friendly markdown cards:
    start    — a new training or offline-evaluation job appeared
    finished — job succeeded (or progress reached 100%)
    stalled  — progress has not advanced for the warning threshold
    failed   — job failed / log error / critical stall

Routine status is generated hourly by the watchdog scheduler.

Standalone: reads kjob logs + kubectl only; never imports training code.
"""

from __future__ import annotations

import re
import json
import time
import pathlib
import datetime as dt
import subprocess

from tools.config import atomic_json, local_root, workspace_root

KJOB_LOG = workspace_root() / "Pi" / "kjob_logs"
OFFLINE_STATUS_SCHEMA = "pi.offline_eval_status.v1"
OFFLINE_STATUS_DIR = "offline_eval_status"
STATE_FILE = local_root() / "tools" / "watchdog-train" / "cursors.json"
TRAIN_PATTERNS = []
KUBECTL_ARGS = []
ERROR_MARKERS = (
    "Traceback",
    "traceback",
    "Error",
    "ERROR",
    "RuntimeError",
    "OOM",
    "CUDA out of memory",
)
STALL_WARNING_MIN = 15
STALL_FAIL_MIN = 60


def _run(cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return (result.stdout or result.stderr).strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# kubectl job/pod facts
# ---------------------------------------------------------------------------


def _kubectl_jobs() -> dict[str, dict]:
    """job_name -> {status, start_time, completion_time, exp_name}."""
    out = _run(["kubectl", *KUBECTL_ARGS, "get", "jobs", "-o", "json"])
    jobs: dict[str, dict] = {}
    try:
        data = json.loads(out)
        for item in data.get("items", []):
            name = item["metadata"]["name"]
            status = item.get("status", {})
            if status.get("succeeded", 0) > 0:
                phase = "succeeded"
            elif status.get("failed", 0) > 0:
                phase = "failed"
            elif status.get("active", 0) > 0:
                phase = "active"
            else:
                phase = "pending"
            jobs[name] = {
                "status": phase,
                "start_time": item["metadata"].get("creationTimestamp", ""),
                # kjob records the launch script here; used for deterministic
                # exp-name resolution (see below).
                "script": item["metadata"]
                .get("annotations", {})
                .get("kjobctl.x-k8s.io/script", ""),
                "exp_name": "",  # resolved from the script first, then legacy mtime matching
            }
    except Exception:  # noqa: BLE001
        pass
    # Associate each job with a training/eval name by matching its submit
    # timestamp against kjob log files. The experiment identity lives inside
    # the container shell and is invisible to Kubernetes.
    import time as _time
    import calendar

    def _exp_by_timestamp(ts: str) -> tuple[str, pathlib.Path | None]:
        # creationTimestamp is UTC; timegm keeps UTC semantics so it is
        # comparable with log mtimes (absolute epochs).
        try:
            job_time = calendar.timegm(_time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            return "", None
        best = ""
        best_delta = 3600  # 1h window
        best_log = None
        for name, pattern in TRAIN_PATTERNS:
            for log in KJOB_LOG.glob(pattern + ".out"):
                if log in taken:
                    continue
                delta = abs(log.stat().st_mtime - job_time)
                if delta < best_delta:
                    best_delta = delta
                    best = name
                    best_log = log
        for log in KJOB_LOG.glob("offline-eval-*.out"):
            if log in taken:
                continue
            delta = abs(log.stat().st_mtime - job_time)
            if delta < best_delta:
                best_delta = delta
                best = log.stem
                best_log = log
        return best, best_log

    # Preferred path: resolve the exp name from the launch script recorded in
    # the kjob annotation (deterministic pairing, no mtime guessing). Only
    # training exp scripts are eligible; everything else falls through to the
    # legacy mtime matching below.
    for job in jobs.values():
        script_rel = job.get("script", "")
        if "/exp/" not in script_rel:
            continue
        try:
            text = (KJOB_LOG.parent / script_rel).read_text(errors="ignore")
        except OSError:
            continue
        match = re.search(r"export\s+EXP_NAME\s*=\s*[\"']?([^\"'\s]+)", text)
        if match:
            job["exp_name"] = match.group(1)
            continue
        # Serial launchers (e.g. launch_serial_abc.sh) set EXP_NAME only in the
        # sub-scripts they chain. Resolve the active phase: parse each
        # referenced sub-script's EXP_NAME and pick the one whose .out log has
        # the newest mtime (the 1h timestamp window below cannot pair a
        # multi-day serial job with its phase logs).
        sub_scripts = re.findall(r'zsh\s+"?\$SCRIPT_DIR/([^"\s]+\.sh)"?', text)
        best_name, best_mtime = "", 0.0
        for sub in sub_scripts:
            try:
                sub_text = (
                    (KJOB_LOG.parent / script_rel)
                    .parent.joinpath(sub)
                    .read_text(errors="ignore")
                )
            except OSError:
                continue
            sub_match = re.search(
                r"export\s+EXP_NAME\s*=\s*[\"']?([^\"'\s]+)", sub_text
            )
            if not sub_match:
                continue
            for log in KJOB_LOG.glob(f"jobs-*-{sub_match.group(1)}.out"):
                if log.stat().st_mtime > best_mtime:
                    best_mtime = log.stat().st_mtime
                    best_name = sub_match.group(1)
        if best_name:
            for display, pattern in TRAIN_PATTERNS:
                if pattern.strip("*") in best_name:
                    job["exp_name"] = display
                    break
            else:
                job["exp_name"] = best_name

    # Greedy assignment, oldest job first: each log file maps to one job only.
    # Two jobs submitted seconds apart (e.g. GOAI balanced + frame) otherwise
    # both resolve to the log with the closest mtime and collide.
    taken: set[pathlib.Path] = set()
    for job in sorted(jobs.values(), key=lambda j: j["start_time"]):
        if job.get("exp_name"):
            continue
        exp, log = _exp_by_timestamp(job["start_time"])
        job["exp_name"] = exp
        if log is not None:
            taken.add(log)
    return jobs


# ---------------------------------------------------------------------------
# log parsing
# ---------------------------------------------------------------------------


def _latest_err(pattern: str) -> pathlib.Path | None:
    errs = list(KJOB_LOG.glob(pattern + ".err"))
    if not errs:
        return None
    return max(errs, key=lambda p: p.stat().st_mtime)


def _latest_train_err(exp_name: str) -> pathlib.Path | None:
    """Return the rank-0 training log for a raw experiment name."""
    single_node_suffix = f"-{exp_name}.err"
    multi_node_prefix = f"jobs-{exp_name}-node0-"
    candidates = [
        path
        for path in KJOB_LOG.glob("jobs-*.err")
        if path.name.endswith(single_node_suffix)
        or path.name.startswith(multi_node_prefix)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _parse_elapsed_h(elapsed: str) -> float:
    parts = [int(p) for p in elapsed.split(":")]
    if len(parts) == 3:
        return parts[0] + parts[1] / 60 + parts[2] / 3600
    if len(parts) == 2:
        return parts[0] / 60 + parts[1] / 3600
    return parts[0] / 3600


def _tqdm_fields(err_path: pathlib.Path) -> dict | None:
    text = err_path.read_text(errors="ignore")
    matches = re.findall(
        r"FSDP-Train:\s+\S+\s+\|\s+(\d+)/(\d+)\s+\[(\d+(?::\d+){1,2})<(\d+(?::\d+){1,2}),\s+(\d+\.\d+)(s/it|it/s),\s*loss=([\d.e-]+),\s*lr=([\d.e-]+)",
        text,
    )
    if not matches:
        return None
    step, total, elapsed, _remaining, rate, unit, loss, lr = matches[-1]
    # tqdm prints "s/it" below 1 it/s and "it/s" above; normalize to it/s.
    its = float(rate) if unit == "it/s" else 1.0 / float(rate)
    spe = re.search(r"epochs=(\d+)\s+steps_per_epoch=(\d+)", text)
    return {
        "type": "train",
        "step": int(step),
        "total": int(total),
        "elapsed_h": _parse_elapsed_h(elapsed),
        "its": its,
        "loss": loss,
        "lr": lr,
        "epoch": (int(step) / int(spe.group(2)), int(spe.group(1)))
        if spe
        else (None, None),
    }


def _iso_timestamp(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _offline_progress(exp_name: str, *, now: float | None = None) -> dict | None:
    status_path = KJOB_LOG / OFFLINE_STATUS_DIR / f"{exp_name}.json"
    try:
        payload = json.loads(status_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema") != OFFLINE_STATUS_SCHEMA:
        return None

    counts = payload.get("counts", {})
    try:
        total = int(counts["total"])
        pending = int(counts["pending"])
        running = int(counts["running"])
        complete = int(counts["complete"])
        skipped = int(counts["skipped"])
        failed = int(counts["failed"])
    except (KeyError, TypeError, ValueError):
        return None

    now = time.time() if now is None else now
    started_at = _iso_timestamp(payload.get("started_at"))
    finished_at = _iso_timestamp(payload.get("finished_at"))
    elapsed_seconds = max(0.0, (finished_at or now) - started_at) if started_at else 0.0
    jobs = payload.get("jobs", {})
    if not isinstance(jobs, dict):
        jobs = {}
    finished_durations = [
        float(job["elapsed_seconds"])
        for job in jobs.values()
        if isinstance(job, dict)
        and job.get("status") in {"complete", "failed"}
        and isinstance(job.get("elapsed_seconds"), (int, float))
    ]
    remaining = pending + running
    max_workers = max(1, int(payload.get("plan", {}).get("max_workers", 1)))
    if payload.get("status") in {"complete", "failed"}:
        remaining_hours = 0.0
    elif finished_durations:
        average_seconds = sum(finished_durations) / len(finished_durations)
        remaining_hours = average_seconds * remaining / max_workers / 3600
    else:
        remaining_hours = None

    running_jobs = [
        job_id
        for job_id, job in jobs.items()
        if isinstance(job, dict) and job.get("status") == "running"
    ]
    activity_times = [status_path.stat().st_mtime]
    for job_id in running_jobs:
        log_value = jobs[job_id].get("log")
        if not isinstance(log_value, str):
            continue
        try:
            activity_times.append(pathlib.Path(log_value).stat().st_mtime)
        except OSError:
            pass

    return {
        "type": "offline_eval",
        "request_id": str(payload.get("request_id", "")),
        "status": str(payload.get("status", "")),
        "step": complete + skipped + failed,
        "total": total,
        "pending": pending,
        "running": running,
        "complete": complete,
        "skipped": skipped,
        "failed": failed,
        "elapsed_h": elapsed_seconds / 3600,
        "remaining_h": remaining_hours,
        "running_jobs": running_jobs,
        "activity_age_min": max(0.0, (now - max(activity_times)) / 60),
    }


def _load_state() -> dict[str, int]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:  # noqa: BLE001
            pass
    return {}


def _save_state(state: dict[str, int]) -> None:
    atomic_json(STATE_FILE, state)


# ---------------------------------------------------------------------------
# message building
# ---------------------------------------------------------------------------


def _event_body(
    job_id: str,
    exp_name: str,
    status: str,
    progress: dict | None,
    extra: list[tuple[str, str]] = (),
) -> str:
    # Same title + bullet layout as the heartbeat status(): name in the
    # title line, then kjob and progress details as bullets (no markdown
    # tables — cramped on phones).
    title = exp_name or job_id or "训练任务"
    kind = _job_kind(exp_name)
    lines = [f"**{title}** - {status}（{kind}）"]
    if job_id:
        lines.append(f"- kjob: {job_id}")
    if progress:
        _append_progress(lines, progress, precise=True)
    for key, value in extra:
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _job_kind(exp_name: str) -> str:
    if exp_name.startswith("offline-eval"):
        return "评测"
    if exp_name:
        return "训练"
    return "未知"


def _append_progress(lines: list[str], progress: dict, *, precise: bool) -> None:
    if progress.get("type") == "offline_eval":
        total = progress["total"]
        pct = progress["step"] / total * 100 if total else 0.0
        request_id = progress.get("request_id")
        if request_id:
            lines.append(f"- 请求: {request_id}")
        lines.append(f"- 进度 job: {progress['step']}/{total} ({pct:.1f}%)")
        lines.append(f"- 运行中: {progress['running']} / 失败: {progress['failed']}")
        remaining_h = progress.get("remaining_h")
        eta = f"{remaining_h:.1f}h" if remaining_h is not None else "计算中"
        lines.append(f"- ETA: 已跑 {progress['elapsed_h']:.1f}h / 剩 {eta}")
        running_jobs = progress.get("running_jobs", [])
        if running_jobs:
            visible = ", ".join(running_jobs[:3])
            if len(running_jobs) > 3:
                visible += f" 等 {len(running_jobs)} 个"
            lines.append(f"- 当前: {visible}")
        return

    epoch_s = (
        f"{progress['epoch'][0]:.2f}/{progress['epoch'][1]}"
        if progress["epoch"][0] is not None and precise
        else f"{progress['epoch'][0]:.1f}/{progress['epoch'][1]}"
        if progress["epoch"][0] is not None
        else "?"
    )
    pct = progress["step"] / progress["total"] * 100
    remaining_h = (progress["total"] - progress["step"]) / progress["its"] / 3600
    pct_text = f"{pct:.1f}" if precise else f"{pct:.0f}"
    lines.append(f"- 进度 epoch: {epoch_s}")
    lines.append(f"- 进度 step: {progress['step']}/{progress['total']} ({pct_text}%)")
    lines.append(f"- ETA: 已跑 {progress['elapsed_h']:.1f}h / 剩 {remaining_h:.1f}h")
    lines.append(f"- Loss: {progress['loss']}")


# ---------------------------------------------------------------------------
# monitor
# ---------------------------------------------------------------------------


class TrainMonitor:
    name = "train"
    alert_interval = 120
    status_interval = 3600

    def __init__(self, sender=None) -> None:
        self.sender = sender or (lambda kind, body: False)
        self._err_state = _load_state()
        self._known_jobs: dict[str, str] = {}  # job_name -> last phase notified
        self._stalled_reported: dict[str, str] = {}
        self._initialized = False  # first scan builds baseline, emits nothing

    # -- event scan ----------------------------------------------------------
    def check_alerts(self) -> list:
        events: list[
            tuple[str, str, str, dict | None, list]
        ] = []  # (kind, job_id, exp, progress, extra)

        jobs = _kubectl_jobs()
        # 1. start / finished / failed by job phase transitions
        for job_name, job in jobs.items():
            phase = job["status"]
            prev = self._known_jobs.get(job_name)
            exp = job.get("exp_name", "")
            if prev is None and phase == "active":
                self._known_jobs[job_name] = "running"
                if not self._initialized:
                    continue  # baseline: existed before we started watching
                events.append(("start", job_name, exp, self._progress_of(exp), []))
            elif prev in ("running", "active") and phase == "succeeded":
                self._known_jobs[job_name] = "finished"
                events.append(("finished", job_name, exp, self._progress_of(exp), []))
            elif prev in ("running", "active") and phase == "failed":
                self._known_jobs[job_name] = "failed"
                events.append(("failed", job_name, exp, self._progress_of(exp), []))
            elif prev is None and phase == "failed":
                # appeared already failed (e.g. quick crash)
                self._known_jobs[job_name] = "failed"
                if not self._initialized:
                    continue
                events.append(("failed", job_name, exp, self._progress_of(exp), []))
        self._initialized = True
        # 2. log errors -> failed
        for kind, _, exp, progress, extra in self._scan_err_events():
            events.append((kind, "", exp, progress, extra))
        # 3. stall -> failed (after critical threshold)
        for kind, _, exp, progress, extra in self._check_stall_events(jobs):
            events.append((kind, "", exp, progress, extra))

        for kind, job_id, exp, progress, extra in events:
            self._emit(kind, job_id, exp, progress, extra)
        return []

    # -- log err scan --------------------------------------------------------
    def _scan_err_events(self):
        events = []
        # 训练日志 jobs-*.err + 离线评测日志 offline-eval-*.err（P5 接入）
        err_files = sorted(
            list(KJOB_LOG.glob("jobs-*.err"))
            + list(KJOB_LOG.glob("offline-eval-*.err"))
        )
        for err_path in err_files:
            key = str(err_path)
            size = err_path.stat().st_size
            if key not in self._err_state:
                self._err_state[key] = size
                continue
            offset = self._err_state[key]
            if size <= offset:
                self._err_state[key] = size
                continue
            with err_path.open("rb") as fh:
                fh.seek(offset)
                new_text = fh.read(size - offset).decode(errors="ignore")
            self._err_state[key] = size
            for marker in ERROR_MARKERS:
                if marker in new_text:
                    line = next(
                        (ln for ln in new_text.splitlines() if marker in ln), marker
                    )
                    exp = self._exp_from_err_path(err_path)
                    events.append(
                        (
                            "failed",
                            "",
                            exp,
                            self._progress_of(exp),
                            [("错误", line.strip()[:200])],
                        )
                    )
                    break
        _save_state(self._err_state)
        return events

    # -- stall ---------------------------------------------------------------
    def _check_stall_events(self, jobs: dict[str, dict]):
        events = []
        now = time.time()
        active_experiments = {
            str(job.get("exp_name", ""))
            for job in jobs.values()
            if job.get("status") == "active" and job.get("exp_name")
        }
        patterns = dict(TRAIN_PATTERNS)
        for name in list(self._stalled_reported):
            if name not in active_experiments:
                self._stalled_reported.pop(name, None)
        for name in sorted(active_experiments):
            if name.startswith("offline-eval-"):
                continue
            err_path = (
                _latest_err(patterns[name])
                if name in patterns
                else _latest_train_err(name)
            )
            if err_path is None:
                continue
            age_min = (now - err_path.stat().st_mtime) / 60
            if _tqdm_fields(err_path) is None:
                continue
            previous = self._stalled_reported.get(name)
            if age_min >= STALL_FAIL_MIN:
                if previous != "failed":
                    events.append(
                        (
                            "failed",
                            "",
                            name,
                            _tqdm_fields(err_path),
                            [("故障", f"进度 {age_min:.0f} 分钟未更新")],
                        )
                    )
                self._stalled_reported[name] = "failed"
            elif age_min >= STALL_WARNING_MIN:
                if previous not in {"warning", "failed"}:
                    events.append(
                        (
                            "stalled",
                            "",
                            name,
                            _tqdm_fields(err_path),
                            [("停滞", f"进度 {age_min:.0f} 分钟未更新")],
                        )
                    )
                if previous != "failed":
                    self._stalled_reported[name] = "warning"
            else:
                self._stalled_reported.pop(name, None)
        active_offline = {
            exp for exp in active_experiments if exp.startswith("offline-eval-")
        }
        for exp in list(self._stalled_reported):
            if exp.startswith("offline-eval-") and exp not in active_offline:
                self._stalled_reported.pop(exp, None)
        for exp in sorted(active_offline):
            progress = self._progress_of(exp)
            if not progress:
                continue
            age_min = progress["activity_age_min"]
            previous = self._stalled_reported.get(exp)
            if age_min >= STALL_FAIL_MIN:
                if previous != "failed":
                    events.append(
                        (
                            "failed",
                            "",
                            exp,
                            progress,
                            [("故障", f"进度 {age_min:.0f} 分钟未更新")],
                        )
                    )
                self._stalled_reported[exp] = "failed"
            elif age_min >= STALL_WARNING_MIN:
                if previous not in {"warning", "failed"}:
                    events.append(
                        (
                            "stalled",
                            "",
                            exp,
                            progress,
                            [("停滞", f"进度 {age_min:.0f} 分钟未更新")],
                        )
                    )
                if previous != "failed":
                    self._stalled_reported[exp] = "warning"
            else:
                self._stalled_reported.pop(exp, None)
        return events

    # -- helpers -------------------------------------------------------------
    def _exp_from_err_path(self, err_path: pathlib.Path) -> str:
        for name, pattern in TRAIN_PATTERNS:
            if pathlib.PurePath(err_path).match(pattern + ".err"):
                return name
        if err_path.name.startswith("offline-eval"):
            return err_path.stem  # offline-eval-<job-id>（离线评测任务）
        return err_path.name

    def _progress_of(self, exp_name: str) -> dict | None:
        if not exp_name:
            return None
        if exp_name.startswith("offline-eval-"):
            return _offline_progress(exp_name)
        for name, pattern in TRAIN_PATTERNS:
            if name == exp_name or pattern.strip("*") in exp_name:
                err_path = _latest_err(pattern)
                return _tqdm_fields(err_path) if err_path else None
        # Jobs resolved via the kjob script annotation use raw experiment
        # names; resolve either the single-node log or the multi-node node0 log.
        err_path = _latest_train_err(exp_name)
        return _tqdm_fields(err_path) if err_path else None

    def _emit(
        self, kind: str, job_id: str, exp: str, progress: dict | None, extra: list
    ) -> None:
        status = {
            "start": "开始",
            "finished": "结束",
            "failed": "故障",
            "stalled": "停滞",
        }.get(kind, kind)
        body = _event_body(job_id, exp, status, progress, extra)
        sent = self.sender(kind, body)
        outcome = "sent" if sent else "send failed"
        print(f"[watchdog] {kind} event {outcome}: {exp or job_id}", flush=True)

    # -- routine heartbeat: running kjobs as title + bullet list (mobile-
    #    friendly; markdown tables are cramped on phones). Finished/failed
    #    jobs already got their dedicated start/finish/failure cards.
    def status(self) -> str:
        jobs = _kubectl_jobs()
        if not jobs:
            return ""
        active_jobs = sorted(
            (name, job) for name, job in jobs.items() if job["status"] == "active"
        )
        blocks: list[str] = []
        for index, (name, job) in enumerate(active_jobs, start=1):
            exp = job.get("exp_name", "")
            kind = _job_kind(exp)
            progress = self._progress_of(exp) if exp else None
            lines = [
                f"**job{index}: {exp or name}** - 运行中（{kind}）",
                f"- kjob: {name}",
            ]
            if progress:
                _append_progress(lines, progress, precise=False)
            blocks.append("\n".join(lines))
        if not blocks:
            return ""
        return time.strftime("🕐 %Y-%m-%d %H:%M\n\n") + "\n\n".join(blocks)
