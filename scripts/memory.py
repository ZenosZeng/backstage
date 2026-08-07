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
    if event.get("event_type") not in EVENT_TYPES:
        errors.append(f"未知 event_type：{event.get('event_type')}")
    if event.get("status") not in STATUSES:
        errors.append(f"未知 status：{event.get('status')}")
    if event.get("event_type") == "hypothesis" and event.get("status") != "needs_verification":
        errors.append("hypothesis 必须使用 needs_verification 状态")
    for key in ("machine_id", "agent"):
        value = str(event.get(key, ""))
        if not SAFE_NAME.fullmatch(value):
            errors.append(f"{key} 格式无效")
    try:
        dt.datetime.fromisoformat(str(event.get("created_at", "")).replace("Z", "+00:00"))
    except ValueError:
        errors.append("created_at 不是有效 ISO-8601 时间")
    if not isinstance(event.get("content"), dict):
        errors.append("content 必须是 object")
    if not isinstance(event.get("projects"), list):
        errors.append("projects 必须是 array")
    if contains_sensitive_value(event):
        errors.append("事件疑似包含 secret")
    return errors


def daily_path(root: Path, event: dict[str, Any]) -> Path:
    date = str(event["created_at"])[:10]
    return root / "memory" / str(event["machine_id"]) / str(event["agent"]) / f"{date}.json"


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
    grouped: dict[Path, list[dict[str, Any]]] = {}
    for raw in events:
        event = normalize_event(raw)
        errors = validate_event(event)
        if errors:
            raise ValueError(f"{event.get('event_id', '<unknown>')}：{'；'.join(errors)}")
        grouped.setdefault(daily_path(root, event), []).append(event)

    added = 0
    duplicates = 0
    for path, incoming in grouped.items():
        machine, agent, filename = path.parts[-3:]
        date = filename.removesuffix(".json")
        with file_lock(root, machine, agent, date):
            if path.exists():
                document = read_json(path)
            else:
                document = {
                    "schema_version": 1,
                    "machine_id": machine,
                    "agent": agent,
                    "date": date,
                    "events": [],
                }
            if not isinstance(document, dict) or not isinstance(document.get("events"), list):
                raise ValueError(f"每日记忆文件格式无效：{path}")
            if document.get("machine_id") != machine or document.get("agent") != agent:
                raise ValueError(f"每日记忆文件身份不匹配：{path}")
            existing = {event.get("event_id") for event in document["events"]}
            for event in incoming:
                if event["event_id"] in existing:
                    duplicates += 1
                    continue
                document["events"].append(event)
                existing.add(event["event_id"])
                added += 1
            document["events"].sort(key=lambda item: (item["created_at"], item["event_id"]))
            atomic_write_json(path, document)
    return added, duplicates


def memory_files(root: Path) -> list[Path]:
    return sorted((root / "memory").glob("*/*/*.json"))


def load_events(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in memory_files(root):
        try:
            document = read_json(path)
            values = document["events"]
            if not isinstance(values, list):
                raise ValueError("events 不是 array")
            for raw in values:
                event = normalize_event(raw)
                item_errors = validate_event(event)
                if item_errors:
                    errors.append(f"{path}:{event.get('event_id', '?')}：{'；'.join(item_errors)}")
                events.append(event)
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}：{exc}")
    events.sort(key=lambda item: (item.get("created_at", ""), item.get("event_id", "")))
    return events, errors


def render_event(event: dict[str, Any]) -> str:
    projects = ", ".join(project.get("name", "?") for project in event["projects"]) or "-"
    what = event.get("content", {}).get("what", "")
    lines = [
        f"[{event['created_at']}] {event['title']}",
        f"  {event['machine_id']}/{event['agent']} | {event['event_type']}/{event['status']} | {projects}",
        f"  topic={event['topic_key']} task={event.get('task_id') or '-'} id={event['event_id']}",
    ]
    if what:
        lines.append(f"  {what}")
    return "\n".join(lines)


def select_events(events: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    selected = events
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
    return selected[: args.limit]


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
        print("\n\n".join(render_event(event) for event in selected))
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
    counts = Counter(event.get("event_id") for event in events)
    errors.extend(f"重复 event_id：{event_id}" for event_id, count in counts.items() if count > 1)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        print(f"校验失败：{len(events)} 条事件，{len(errors)} 个错误", file=sys.stderr)
        return 1
    print(f"校验通过：{len(memory_files(root))} 个 daily JSON，{len(events)} 条事件")
    return 0


def add_filters(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project")
    parser.add_argument("--task")
    parser.add_argument("--agent")
    parser.add_argument("--limit", type=int, default=20)
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
    search.set_defaults(func=command_list)

    recent = subparsers.add_parser("recent", help="读取最近记忆")
    add_filters(recent)
    recent.set_defaults(func=command_list)

    import_events = subparsers.add_parser("import", help="幂等导入 event array")
    import_events.add_argument("--input", required=True)
    import_events.set_defaults(func=command_import)

    status = subparsers.add_parser("status", help="显示记忆库状态")
    status.set_defaults(func=command_status)

    validate = subparsers.add_parser("validate", help="校验全部 daily JSON")
    validate.set_defaults(func=command_validate)
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
