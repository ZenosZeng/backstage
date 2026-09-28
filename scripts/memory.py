#!/usr/bin/env python3
"""Filesystem-first shared memory for Codex and Claude Code."""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import sys
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
EVENT_TYPES = {
    "fact",
    "decision",
    "progress",
    "bugfix",
    "config",
    "risk",
    "hypothesis",
    "test_result",
    "preference",
}
STATUSES = {"active", "superseded", "resolved", "needs_verification"}
TASK_STATES = {"active", "paused", "closed"}
SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
}
SECRET_PATTERNS = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
)
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value: dt.datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(dt.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(root: Path) -> dict[str, Any]:
    path = root / "config.json"
    try:
        config = read_json(path)
    except FileNotFoundError as exc:
        raise ValueError(f"缺少配置文件：{path}") from exc
    if not isinstance(config, dict):
        raise ValueError(f"配置文件必须是 JSON object：{path}")
    for key in ("workspace", "workspace_root", "machine_id"):
        if key not in config:
            raise ValueError(f"配置文件缺少字段：{key}")
    if not SAFE_NAME.fullmatch(str(config["machine_id"])):
        raise ValueError("machine_id 只能包含字母、数字、点、下划线和连字符")
    return config


def workspace_root(config: dict[str, Any]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(config["workspace_root"]))))


def git_value(path: Path, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


def project_metadata(config: dict[str, Any], names: list[str]) -> list[dict[str, Any]]:
    root = workspace_root(config)
    registry = config.get("projects", {})
    if not isinstance(registry, dict):
        raise ValueError("config.json 中的 projects 必须是 JSON object")
    result: list[dict[str, Any]] = []
    for name in dict.fromkeys(names):
        item = registry.get(name)
        if not isinstance(item, dict) or not item.get("path"):
            raise ValueError(f"未知 project：{name}；请先加入 config.json")
        path_hint = str(item["path"])
        path = root / path_hint
        branch = git_value(path, "branch", "--show-current") if path.exists() else None
        commit = git_value(path, "rev-parse", "HEAD") if path.exists() else None
        dirty = False
        if commit is not None:
            dirty = bool(git_value(path, "status", "--short"))
        result.append(
            {
                "name": name,
                "path_hint": path_hint,
                "branch": branch,
                "commit": commit,
                "dirty": dirty,
            }
        )
    return result


def event_projects(event: dict[str, Any]) -> list[dict[str, Any]]:
    projects = event.get("projects", event.get("repos", []))
    if not isinstance(projects, list):
        return []
    normalized: list[dict[str, Any]] = []
    for project in projects:
        if isinstance(project, str):
            normalized.append({"name": project})
        elif isinstance(project, dict):
            normalized.append(dict(project))
    return normalized


def normalize_event(event: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise ValueError("event 必须是 object")
    projects = event.get("projects", event.get("repos", []))
    if not isinstance(projects, list) or any(
        not isinstance(item, (str, dict)) for item in projects
    ):
        raise ValueError("projects/repos 必须是 project name 或 object 的 array")
    value = dict(event)
    value["projects"] = event_projects(value)
    value.pop("repos", None)
    value.setdefault("workspace", "code")
    value.setdefault("scope", "task" if value.get("task_id") else "workspace")
    value.setdefault("sensitivity", "normal")
    value.setdefault("supersedes", [])
    value.setdefault("status", "active")
    value.setdefault("content", {})
    return value


def contains_sensitive_value(value: Any, key: str | None = None) -> bool:
    if key and key.lower().replace("-", "_") in SENSITIVE_KEYS and value not in (None, ""):
        return True
    if isinstance(value, dict):
        return any(contains_sensitive_value(item, str(name)) for name, item in value.items())
    if isinstance(value, list):
        return any(contains_sensitive_value(item) for item in value)
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in SECRET_PATTERNS)
    return False


def validate_event(event: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    required = (
        "schema_version",
        "event_id",
        "created_at",
        "machine_id",
        "agent",
        "workspace",
        "event_type",
        "topic_key",
        "title",
        "content",
        "status",
        "projects",
    )
    for key in required:
        if key not in event:
            errors.append(f"缺少字段 {key}")
    if type(event.get("schema_version")) is not int or event["schema_version"] != 1:
        errors.append("仅支持 schema_version=1")
    for key in ("event_id", "title", "topic_key", "workspace"):
        if not isinstance(event.get(key), str) or not event[key].strip():
            errors.append(f"{key} 必须是非空字符串")
    if not isinstance(event.get("event_type"), str) or event["event_type"] not in EVENT_TYPES:
        errors.append("未知 event_type")
    if not isinstance(event.get("status"), str) or event["status"] not in STATUSES:
        errors.append("未知 status")
    if event.get("event_type") == "hypothesis" and event.get("status") != "needs_verification":
        errors.append("hypothesis 必须使用 needs_verification 状态")
    for key in ("machine_id", "agent"):
        value = event.get(key, "")
        if not isinstance(value, str) or not SAFE_NAME.fullmatch(value):
            errors.append(f"{key} 格式无效")
    try:
        parse_time(str(event.get("created_at", "")))
    except ValueError:
        errors.append("created_at 不是有效 ISO-8601 时间")
    if not isinstance(event.get("content"), dict):
        errors.append("content 必须是 object")
    elif "task_state" in event["content"]:
        state = event["content"]["task_state"]
        if not isinstance(state, str) or state not in TASK_STATES:
            errors.append("task_state 必须是 active、paused 或 closed")
        if not isinstance(event.get("task_id"), str) or not event["task_id"].strip():
            errors.append("任务状态事件必须有 task_id")
        if event.get("event_type") != "decision":
            errors.append("任务状态事件必须使用 decision 类型")
        if not event["content"].get("what") or not event["content"].get("where"):
            errors.append("任务状态事件必须记录原因 what 和来源 where")
    if not isinstance(event.get("projects"), list):
        errors.append("projects 必须是 array")
    supersedes = event.get("supersedes", [])
    if not isinstance(supersedes, list) or any(
        not isinstance(item, str) or not item for item in supersedes
    ):
        errors.append("supersedes 必须是非空 event_id 的 array")
    elif event.get("event_id") in supersedes:
        errors.append("事件不能 supersede 自己")
    if contains_sensitive_value(event):
        errors.append("事件疑似包含 secret")
    return errors


def parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return parsed.astimezone(dt.timezone.utc)


def read_daily(path: Path) -> dict[str, Any]:
    document = read_json(path)
    machine, agent, filename = path.parts[-3:]
    dt.date.fromisoformat(filename.removesuffix(".json"))
    if not isinstance(document, dict) or not isinstance(document.get("events"), list):
        raise ValueError(f"daily JSON 格式无效：{path}")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1 or (
        document.get("machine_id"), document.get("agent"), document.get("date")
    ) != (machine, agent, filename.removesuffix(".json")):
        raise ValueError(f"daily JSON 身份或日期不匹配：{path}")
    seen = set()
    for raw in document["events"]:
        event = normalize_event(raw)
        errors = validate_event(event)
        if errors:
            raise ValueError(f"daily JSON 事件校验失败：{path}：{'；'.join(errors)}")
        if (event["machine_id"], event["agent"]) != (machine, agent):
            raise ValueError(f"事件与 daily JSON 身份不匹配：{path}")
        if event["event_id"] in seen:
            raise ValueError(f"daily JSON 包含重复 event_id：{path}")
        seen.add(event["event_id"])
    return document


@contextmanager
def operation_lock(root: Path, name: str) -> Iterator[None]:
    path = root / ".local" / ".locks" / f"{name}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def daily_path(root: Path, event: dict[str, Any]) -> Path:
    date = str(event["created_at"])[:10]
    return root / ".share" / "memory" / str(event["machine_id"]) / str(event["agent"]) / f"{date}.json"


@contextmanager
def file_lock(root: Path, machine: str, agent: str, date: str) -> Iterator[None]:
    lock_path = root / ".local" / ".locks" / machine / agent / f"{date}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def append_events(root: Path, events: list[dict[str, Any]]) -> tuple[int, int]:
    # Serialize local imports with pull installation, but never hold this lock during S3 I/O.
    with operation_lock(root, "memory-write"):
        return _append_events(root, events)


def _append_events(root: Path, events: list[dict[str, Any]]) -> tuple[int, int]:
    stored, errors = load_events(root)
    if errors:
        raise ValueError("现有记忆校验失败，请先运行 validate")
    existing = {event["event_id"]: event for event in stored}
    grouped: dict[Path, list[dict[str, Any]]] = {}
    duplicates = 0
    for raw in events:
        event = normalize_event(raw)
        errors = validate_event(event)
        if errors:
            raise ValueError(f"事件校验失败：{'；'.join(errors)}")
        if event["event_id"] in existing:
            if existing[event["event_id"]] != event:
                raise ValueError("event_id 内容冲突，拒绝覆盖；请使用新 ID 和 supersedes")
            duplicates += 1
            continue
        existing[event["event_id"]] = event
        grouped.setdefault(daily_path(root, event), []).append(event)

    added = 0
    for path, incoming in grouped.items():
        machine, agent, filename = path.parts[-3:]
        date = filename.removesuffix(".json")
        with file_lock(root, machine, agent, date):
            if path.exists():
                document = read_daily(path)
            else:
                document = {
                    "schema_version": 1,
                    "machine_id": machine,
                    "agent": agent,
                    "date": date,
                    "events": [],
                }
            for event in incoming:
                document["events"].append(event)
                added += 1
            document["events"].sort(key=lambda item: (item["created_at"], item["event_id"]))
            atomic_write_json(path, document)
    return added, duplicates


def memory_files(root: Path) -> list[Path]:
    return sorted((root / ".share" / "memory").glob("*/*/*.json"))


def load_events(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in memory_files(root):
        try:
            document = read_daily(path)
            events.extend(normalize_event(raw) for raw in document["events"])
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}：{exc}")
    counts = Counter(event["event_id"] for event in events)
    errors.extend(f"重复 event_id：{event_id}" for event_id, count in counts.items() if count > 1)
    events.sort(key=lambda item: (parse_time(item["created_at"]), item["event_id"]))
    return events, errors


def short_text(value: Any, size: int) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= size else text[:size - 1] + "…"


def render_event(event: dict[str, Any], summary_chars: int | None = None) -> str:
    projects = ", ".join(project.get("name", "?") for project in event["projects"]) or "-"
    what = event.get("content", {}).get("what", "")
    lines = [
        f"[{event['created_at']}] {short_text(event['title'], summary_chars) if summary_chars else event['title']}",
        f"  {event['machine_id']}/{event['agent']} | {event['event_type']}/{event['status']} | {projects}",
        f"  topic={event['topic_key']} task={event.get('task_id') or '-'} id={event['event_id']}",
    ]
    if what:
        lines.append(f"  {short_text(what, summary_chars) if summary_chars else what}")
    return "\n".join(lines)


def supersedes_cycles(events: list[dict[str, Any]]) -> set[str]:
    graph = {event["event_id"]: event.get("supersedes", []) for event in events}
    cyclic = set()
    for start in graph:
        pending, visited = list(graph[start]), set()
        while pending:
            key = pending.pop()
            if key == start:
                cyclic.add(start)
                break
            if key not in visited:
                visited.add(key)
                pending.extend(graph.get(key, []))
    return cyclic


def select_events(events: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    selected = list(events)
    if getattr(args, "current", False):
        if supersedes_cycles(events):
            raise ValueError("supersedes 存在环，无法筛选当前记忆；请运行 audit 后人工核验")
        superseded = {key for event in events for key in event.get("supersedes", [])}
        selected = [
            event for event in selected
            if event["event_id"] not in superseded and event["status"] != "superseded"
        ]
    for option, key in (("machine", "machine_id"), ("topic", "topic_key")):
        if getattr(args, option, None):
            selected = [event for event in selected if event.get(key) == getattr(args, option)]
    since, until = getattr(args, "since", None), getattr(args, "until", None)
    if since and until and since >= until:
        raise ValueError("--since 必须早于 --until（结束时间不包含在内）")
    if since:
        selected = [event for event in selected if parse_time(event["created_at"]) >= since]
    if until:
        selected = [event for event in selected if parse_time(event["created_at"]) < until]
    if getattr(args, "project", None):
        selected = [
            event
            for event in selected
            if args.project in {project.get("name") for project in event["projects"]}
        ]
    if getattr(args, "task", None):
        selected = [event for event in selected if event.get("task_id") == args.task]
    if getattr(args, "agent", None):
        selected = [event for event in selected if event.get("agent") == args.agent]
    query = getattr(args, "query", None)
    if query:
        terms = query.casefold().split()
        selected = [
            event
            for event in selected
            if all(term in json.dumps(event, ensure_ascii=False).casefold() for term in terms)
        ]
    selected.reverse()
    if query and getattr(args, "sort", "recent") == "relevance":
        selected.sort(key=lambda event: relevance_score(event, terms), reverse=True)
    return selected[: args.limit]


def relevance_score(event: dict[str, Any], terms: list[str]) -> int:
    fields = [
        (event["title"], 8), (event["topic_key"], 6),
        (event.get("task_id") or "", 5),
        (" ".join(p.get("name", "") for p in event["projects"]), 4),
        (json.dumps(event["content"], ensure_ascii=False), 1),
    ]
    return sum(weight for text, weight in fields for term in terms if term in text.casefold())


def task_states(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Project explicit lifecycle heads; ordinary progress never reopens a task."""
    if supersedes_cycles(events):
        raise ValueError("supersedes 存在环，请先运行 audit")
    replaced = {key for event in events for key in event.get("supersedes", [])}
    tasks: dict[str, dict[str, Any]] = {}
    by_id = {event["event_id"]: event for event in events}
    for event in events:
        task = event.get("task_id")
        if not task:
            continue
        item = tasks.setdefault(task, {"task_id": task, "state": "unknown", "heads": [], "projects": []})
        item["projects"] = sorted(set(item["projects"]) | {p["name"] for p in event["projects"]})
        if "task_state" not in event["content"]:
            continue
        for parent_id in event.get("supersedes", []):
            parent = by_id.get(parent_id)
            if parent is None or parent.get("task_id") != task or "task_state" not in parent["content"]:
                raise ValueError(f"任务状态引用无效：{event['event_id']}；父记录必须是同任务状态事件")
        if event["event_id"] not in replaced and event["status"] != "superseded":
            item["heads"].append(event)
    for item in tasks.values():
        if len(item["heads"]) == 1:
            item["state"] = item["heads"][0]["content"]["task_state"]
        elif len(item["heads"]) > 1:
            item["state"] = "conflict"
    return tasks


def command_task(root: Path, args: argparse.Namespace) -> int:
    if not args.task.strip() or not args.reason.strip() or not all(s.strip() for s in args.where):
        raise ValueError("task、reason 和 where 不得为空白")
    config = load_config(root)
    # Read heads and append under the same existing write lock (same-machine writers).
    with operation_lock(root, "memory-write"):
        events, errors = load_events(root)
        if errors:
            raise ValueError("记忆库校验失败，请先运行 validate")
        tasks = task_states(events)
        current = tasks.get(args.task)
        if current is None and args.state != "active":
            raise ValueError("未知任务；先用 active 建立任务或追加带该 task ID 的记忆")
        heads = current["heads"] if current else []
        if len(heads) > 1 and set(args.resolve) != {e["event_id"] for e in heads}:
            raise ValueError("任务状态冲突；核对后用 --resolve 逐个指定全部 head event_id")
        if args.resolve and set(args.resolve) != {e["event_id"] for e in heads}:
            raise ValueError("--resolve 与当前任务 heads 不一致，请重新读取")
        projects = {p["name"]: {"name": p["name"]}
                    for e in events if e.get("task_id") == args.task for p in e["projects"]}
        registered = [name for name in projects if name in config.get("projects", {})]
        # Capture this decision's checkout, not the commit copied from an old event.
        projects.update({p["name"]: p for p in project_metadata(config, registered + args.project)})
        now = utc_now()
        event = {
            "schema_version": 1,
            "event_id": f"evt_{now.strftime('%Y%m%dT%H%M%S.%fZ')}_{config['machine_id']}_{args.agent}_{uuid.uuid4().hex}",
            "created_at": iso_utc(now), "machine_id": config["machine_id"], "agent": args.agent,
            "workspace": config["workspace"], "scope": "task", "task_id": args.task,
            "projects": list(projects.values()), "event_type": "decision",
            "topic_key": f"task/{args.task}/lifecycle", "title": f"任务 {args.task}: {args.state}",
            "content": {"task_state": args.state, "what": args.reason, "where": args.where},
            "status": "active", "supersedes": [e["event_id"] for e in heads], "sensitivity": "normal",
        }
        _append_events(root, [event])
    print(f"任务 {args.task}: {args.state}；来源 {event['event_id']}")
    return 0


def command_brief(root: Path, args: argparse.Namespace) -> int:
    if not args.project and not args.task:
        raise ValueError("brief 至少指定 --project 或 --task")
    events, errors = load_events(root)
    if errors:
        raise ValueError("记忆库校验失败，请先运行 validate")
    states = task_states(events)
    selectors = argparse.Namespace(**vars(args))
    selectors.current, selectors.limit = True, None
    selected = select_events(events, selectors)
    related = [item for task, item in sorted(states.items())
               if (not args.task or task == args.task)
               and (not args.project or args.project in item["projects"])]
    visible = [item for item in related if args.task or args.include_inactive
               or item["state"] not in {"paused", "closed"}]
    hidden = len(related) - len(visible)
    selected = [e for e in selected if args.task or args.include_inactive
                or states.get(e.get("task_id"), {}).get("state") not in {"paused", "closed"}]
    # Exact duplicate content can be collapsed, but preserve every source ID.
    groups: dict[tuple, dict[str, Any]] = {}
    for event in selected:
        if "task_state" in event["content"]:
            continue
        key = (event.get("task_id"), event["topic_key"], event["event_type"], event["status"],
               json.dumps(event["content"], ensure_ascii=False, sort_keys=True))
        if key not in groups:
            groups[key] = {"event": event, "sources": []}
        groups[key]["sources"].append(event["event_id"])
    sections: dict[str, list] = {name: [] for name in ("约定与决策", "事实与验证记录", "当前进展", "风险与待验证", "待办候选")}
    buckets: dict[str, list] = {name: [] for name in sections if name != "待办候选"}
    for group in groups.values():
        event = group["event"]
        kind = event["event_type"]
        section = ("风险与待验证" if kind in {"risk", "hypothesis"} or event["status"] == "needs_verification"
                   else "约定与决策" if kind in {"decision", "preference", "config"}
                   else "当前进展" if kind == "progress" else "事实与验证记录")
        buckets[section].append(group)
    # Share the budget across categories so fresh progress cannot bury constraints.
    picked = []
    for index in range(max((len(b) for b in buckets.values()), default=0)):
        for section, bucket in buckets.items():
            if index < len(bucket) and len(picked) < args.limit:
                picked.append((section, bucket[index]))
        if len(picked) >= args.limit:
            break
    for section, group in picked:
        event = group["event"]
        kind = event["event_type"]
        entry = {"title": short_text(event["title"], args.summary_chars),
                 "text": short_text(event["content"].get("what", ""), args.summary_chars),
                 "sources": group["sources"], "created_at": event["created_at"],
                 "agent": event["agent"], "machine": event["machine_id"],
                 "projects": [p["name"] for p in event["projects"]],
                 "type": kind, "status": event["status"], "task_id": event.get("task_id")}
        sections[section].append(entry)
        task = event.get("task_id")
        if task and states[task]["state"] in {"active", "unknown"} and event["status"] == "active":
            todo = event["content"].get("next", [])
            if todo:
                sections["待办候选"].append({**entry, "text": short_text(todo, args.summary_chars)})
    result = {
        "notice": "仅摘录记忆，不裁决事实；同 topic 并列不表示一致。unknown 任务尚未声明生命周期；待办需核对源码与现场。",
        "tasks": [{"task_id": item["task_id"], "state": item["state"], "projects": item["projects"],
                   "heads": [{"event_id": e["event_id"], "state": e["content"]["task_state"],
                              "created_at": e["created_at"], "agent": e["agent"], "machine": e["machine_id"],
                              "reason": short_text(e["content"]["what"], args.summary_chars),
                              "where": e["content"]["where"]} for e in item["heads"]]} for item in visible],
        "hidden_inactive_tasks": hidden, "omitted_events": max(0, len(groups) - args.limit), "sections": sections,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        lines = [result["notice"], f"隐藏暂停/关闭任务 {hidden}；未展示记录组 {result['omitted_events']}"]
        for item in result["tasks"]:
            lines.append(f"任务 {item['task_id']}: {item['state']}")
            for head in item["heads"]:
                lines.append(f"  [{head['event_id']}] {head['state']}: {head['reason']}")
        for name, entries in sections.items():
            if entries:
                lines.append(f"\n{name}")
            for entry in entries:
                lines.append(f"- {entry['title']} ({entry['created_at']}; {entry['machine']}/{entry['agent']}; {entry['type']}/{entry['status']})\n  {entry['text']}\n  来源: {', '.join(entry['sources'])}")
        output = "\n".join(lines)
        suffix = "\n[输出已截断；用 --max-chars 增大上限、--task 缩小范围或 --json 查看全部摘要。]"
        print(output if len(output) <= args.max_chars else (output[:max(0, args.max_chars - len(suffix))] + suffix)[:args.max_chars])
    return 0


def command_add(root: Path, args: argparse.Namespace) -> int:
    config = load_config(root)
    now = utc_now()
    status = args.status
    if args.event_type == "hypothesis" and status == "active":
        status = "needs_verification"
    event = {
        "schema_version": 1,
        "event_id": f"evt_{now.strftime('%Y%m%dT%H%M%S.%fZ')}_{config['machine_id']}_{args.agent}_{uuid.uuid4().hex}",
        "created_at": iso_utc(now),
        "machine_id": config["machine_id"],
        "agent": args.agent,
        "workspace": config["workspace"],
        "scope": "task" if args.task else ("repo" if args.project else "workspace"),
        "task_id": args.task,
        "projects": project_metadata(config, args.project),
        "event_type": args.event_type,
        "topic_key": args.topic,
        "title": args.title,
        "content": {
            "what": args.what,
            "why": args.why,
            "where": args.where,
            "verified_by": args.verified_by,
            "next": args.next,
        },
        "status": status,
        "supersedes": args.supersedes,
        "sensitivity": "normal",
    }
    added, _ = append_events(root, [event])
    print(f"已记录 {added} 条事件：{event['event_id']}")
    print(daily_path(root, event))
    return 0


def command_import(root: Path, args: argparse.Namespace) -> int:
    document = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if isinstance(document, dict) and isinstance(document.get("events"), list):
        events = document["events"]
    elif isinstance(document, list):
        events = document
    else:
        raise ValueError("导入文件必须是 event array 或包含 events array 的 object")
    added, duplicates = append_events(root, events)
    print(f"导入完成：新增 {added}，重复 {duplicates}")
    return 0


def command_list(root: Path, args: argparse.Namespace) -> int:
    events, errors = load_events(root)
    if errors:
        print("记忆库包含无效文件，请先运行 validate", file=sys.stderr)
        return 2
    selected = select_events(events, args)
    if args.json:
        print(json.dumps(selected, ensure_ascii=False, indent=2))
    elif selected:
        size = args.summary_chars if getattr(args, "summary", False) else None
        print("\n\n".join(render_event(event, size) for event in selected))
    else:
        print("没有匹配的记忆。")
    return 0


def command_status(root: Path, _args: argparse.Namespace) -> int:
    config = load_config(root)
    events, errors = load_events(root)
    by_machine = Counter(event.get("machine_id", "?") for event in events)
    by_agent = Counter(event.get("agent", "?") for event in events)
    print(f"Workspace: {config['workspace']} ({workspace_root(config)})")
    print(f"Current machine: {config['machine_id']} ({config.get('machine_role', '-')})")
    print(f"Daily files: {len(memory_files(root))}")
    print(f"Events: {len(events)}")
    print(f"By machine: {dict(sorted(by_machine.items()))}")
    print(f"By agent: {dict(sorted(by_agent.items()))}")
    print(f"Validation errors: {len(errors)}")
    return 1 if errors else 0


def command_validate(root: Path, _args: argparse.Namespace) -> int:
    events, errors = load_events(root)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"校验失败：{len(events)} 条事件，{len(errors)} 个错误", file=sys.stderr)
        return 1
    print(f"校验通过：{len(memory_files(root))} 个 daily JSON，{len(events)} 条事件")
    return 0


def command_get(root: Path, args: argparse.Namespace) -> int:
    events, errors = load_events(root)
    if errors:
        raise ValueError("记忆库校验失败，请先运行 validate")
    for event in events:
        if event["event_id"] == args.event_id:
            result = {"source": event_sources(root)[event["event_id"]], "event": event}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    print("没有找到该 event_id。", file=sys.stderr)
    return 1


def event_sources(root: Path) -> dict[str, str]:
    sources = {}
    for path in memory_files(root):
        try:
            for event in read_daily(path)["events"]:
                sources[event["event_id"]] = path.relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
    return sources


def command_audit(root: Path, args: argparse.Namespace) -> int:
    events, errors = load_events(root)
    issues = [{"kind": "invalid", "detail": error} for error in errors]
    known = {event["event_id"] for event in events}
    superseded = {key for event in events for key in event.get("supersedes", [])}
    sources = event_sources(root)
    cyclic = supersedes_cycles(events)
    replacements: dict[str, list[str]] = {}
    for event in events:
        for key in event.get("supersedes", []):
            if event["event_id"] not in superseded:
                replacements.setdefault(key, []).append(event["event_id"])
    ambiguous = {key for values in replacements.values() if len(values) > 1 for key in values}
    cutoff = utc_now() - dt.timedelta(days=args.stale_days)
    selected = select_events(events, args)
    for event in selected:
        kinds = []
        content = event["content"]
        if (
            not content.get("verified_by") and not content.get("where")
            and not any(p.get("commit") for p in event["projects"])
        ):
            kinds.append("missing_source")
        if any(key not in known for key in event.get("supersedes", [])):
            kinds.append("dangling_supersedes")
        if event["event_id"] in ambiguous:
            kinds.append("parallel_supersedes_candidate")
        if event["event_id"] in cyclic:
            kinds.append("supersedes_cycle")
        if Path(sources[event["event_id"]]).stem != event["created_at"][:10]:
            kinds.append("legacy_date_mismatch")
        if event["created_at"][-6:] != "+00:00" and not event["created_at"].endswith("Z"):
            kinds.append("legacy_non_utc")
        if (
            event["event_id"] not in superseded
            and event["status"] in {"active", "needs_verification"}
            and event["event_type"] in {"progress", "risk", "hypothesis"}
            and parse_time(event["created_at"]) < cutoff
        ):
            kinds.append("stale_candidate")
        for kind in kinds:
            issues.append({
                "kind": kind, "event_id": event["event_id"],
                "source": sources[event["event_id"]],
            })
    result = {
        "checked_events": len(selected), "issues": issues,
        "counts": dict(Counter(item["kind"] for item in issues)),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"检查 {result['checked_events']} 条事件；仅列候选问题，不自动删除或判定失效。")
        for item in issues:
            print(f"{item['kind']}: {item.get('event_id', item.get('detail', ''))}")
        print(f"汇总：{result['counts']}")
    return 1 if issues else 0


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def filter_time(value: str) -> dt.datetime:
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return parse_time(value + "T00:00:00Z")
        return parse_time(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("使用 YYYY-MM-DD（UTC）或带时区的 ISO-8601 时间") from exc


def add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project")
    parser.add_argument("--task")
    parser.add_argument("--agent")
    parser.add_argument("--machine")
    parser.add_argument("--topic", help="精确 topic_key")
    parser.add_argument("--since", type=filter_time, help="包含起点，日期按 UTC")
    parser.add_argument("--until", type=filter_time, help="不包含终点，日期按 UTC")
    parser.add_argument("--current", action="store_true", help="排除显式被取代事件，不自动判断同 topic 的正误")
    parser.add_argument("--limit", type=positive_int, default=20)
    parser.add_argument("--json", action="store_true")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="简化的跨 Agent、跨机器工作区记忆工具")
    result.add_argument("--root", type=Path, default=ROOT, help=".agents 目录")
    subparsers = result.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser("add", help="原子追加一条事件")
    add.add_argument("--agent", required=True, choices=("codex", "claude", "kimi"))
    add.add_argument("--project", action="append", default=[])
    add.add_argument("--task")
    add.add_argument("--type", dest="event_type", required=True, choices=sorted(EVENT_TYPES))
    add.add_argument("--status", choices=sorted(STATUSES), default="active")
    add.add_argument("--topic", required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--what", required=True)
    add.add_argument("--why", default="")
    add.add_argument("--where", action="append", default=[])
    add.add_argument("--verified-by", action="append", default=[])
    add.add_argument("--next", action="append", default=[])
    add.add_argument("--supersedes", action="append", default=[])
    add.set_defaults(func=command_add)

    search = subparsers.add_parser("search", help="搜索原始记忆")
    search.add_argument("query")
    add_filters(search)
    search.add_argument("--sort", choices=("relevance", "recent"), default="relevance")
    search.add_argument("--summary-chars", type=positive_int, default=240)
    search_format = search.add_mutually_exclusive_group()
    search_format.add_argument("--summary", dest="summary", action="store_true")
    search_format.add_argument("--full", dest="summary", action="store_false")
    search.set_defaults(func=command_list, summary=True)

    recent = subparsers.add_parser("recent", help="读取最近记忆")
    add_filters(recent)
    recent.add_argument("--summary", action="store_true")
    recent.add_argument("--summary-chars", type=positive_int, default=240)
    recent.set_defaults(func=command_list)

    task = subparsers.add_parser("task", help="追加任务生命周期决定，不改历史记录")
    task.add_argument("--task", required=True, help="工作区内唯一、跨 repo 稳定 task ID")
    task.add_argument("--state", required=True, choices=sorted(TASK_STATES))
    task.add_argument("--agent", required=True, choices=("codex", "claude", "kimi"))
    task.add_argument("--project", action="append", default=[])
    task.add_argument("--reason", required=True)
    task.add_argument("--where", action="append", required=True, help="决定依据/来源")
    task.add_argument("--resolve", action="append", default=[], help="明确取代的冲突 head ID；需列全")
    task.set_defaults(func=command_task)

    brief = subparsers.add_parser("brief", help="按项目或任务生成有来源的精简上下文")
    add_filters(brief)
    brief.add_argument("--include-inactive", action="store_true", help="包含暂停/关闭任务的历史，不推荐其待办")
    brief.add_argument("--summary-chars", type=positive_int, default=240)
    brief.add_argument("--max-chars", type=positive_int, default=6000, help="文本总字符上限；JSON 不截断")
    brief.set_defaults(func=command_brief, current=True)

    import_events = subparsers.add_parser("import", help="幂等导入 event array")
    import_events.add_argument("--input", required=True)
    import_events.set_defaults(func=command_import)

    status = subparsers.add_parser("status", help="显示记忆库状态")
    status.set_defaults(func=command_status)

    validate = subparsers.add_parser("validate", help="校验全部 daily JSON")
    validate.set_defaults(func=command_validate)

    get = subparsers.add_parser("get", help="按 ID 读取完整事件及来源文件")
    get.add_argument("event_id")
    get.set_defaults(func=command_get)

    audit = subparsers.add_parser("audit", help="只读检查来源、过期候选及 supersedes 引用")
    add_filters(audit)
    audit.add_argument("--stale-days", type=positive_int, default=60)
    audit.set_defaults(func=command_audit, limit=None)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args.root.resolve(), args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"memory: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
