#!/usr/bin/env python3
"""S3 synchronization for workspace memory and shared Agent state."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import memory as memory_store


ROOT = Path(__file__).resolve().parents[1]
SHARED_DIRS = ("config", "long-term", "skills", "shared_files")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PROXY_KEYS = {
    "all_proxy",
    "ftp_proxy",
    "http_proxy",
    "https_proxy",
    "rsync_proxy",
}
COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")


class SyncError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SyncError(f"缺少配置文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise SyncError(f"JSON 格式错误：{path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SyncError(f"配置必须是 JSON object：{path}")
    return value


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
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


def validate_remote(remote: str) -> str:
    value = remote.strip().rstrip("/")
    if not value or "://" in value or value.startswith("/") or ".." in value.split("/"):
        raise SyncError(f"remote 必须是 mc alias 路径：{remote!r}")
    if "/" not in value or any(character.isspace() for character in value):
        raise SyncError(f"remote 格式无效：{remote!r}")
    return value


def load_config(root: Path, *, require_initialized: bool = True) -> dict[str, Any]:
    config = read_json(root / "config.json")
    for key in ("workspace", "workspace_root", "machine_id", "sync"):
        if key not in config:
            raise SyncError(f"config.json 缺少字段：{key}")
    machine_id = str(config["machine_id"])
    if not SAFE_NAME.fullmatch(machine_id):
        raise SyncError("machine_id 只能包含字母、数字、点、下划线和连字符")
    sync = config["sync"]
    if not isinstance(sync, dict) or not sync.get("enabled"):
        raise SyncError("config.json 中的 sync 未启用")
    if require_initialized and not sync.get("initialized"):
        raise SyncError("S3 同步尚未初始化；请先运行 sync.py init --push 或 init --pull")
    sync["remote"] = validate_remote(str(sync.get("remote", "")))
    return config


def is_shared_writer(config: dict[str, Any]) -> bool:
    return bool(config.get("shared_writer", config.get("long_term_writer", False)))


def require_shared_writer(config: dict[str, Any], *, allow_non_writer: bool) -> None:
    if not is_shared_writer(config) and not allow_non_writer:
        raise SyncError("当前机器不是 shared_writer，禁止上传 Skill、long-term 和共享配置")


def remote_path(remote: str, *parts: str) -> str:
    suffix = "/".join(part.strip("/") for part in parts if part)
    return f"{remote}/{suffix}" if suffix else remote


def mc_environment(clear_proxy: bool) -> dict[str, str]:
    environment = os.environ.copy()
    if clear_proxy:
        for key in list(environment):
            if key.casefold() in PROXY_KEYS:
                environment.pop(key, None)
    return environment


def run_mc(
    arguments: list[str],
    *,
    clear_proxy: bool,
    dry_run: bool,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = ["mc", *arguments]
    if dry_run:
        print(f"DRY-RUN: {shlex.join(command)}")
        return subprocess.CompletedProcess(command, 0, "", "")
    if shutil.which("mc") is None:
        raise SyncError("找不到 mc，请先安装并配置 S3 alias")
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=mc_environment(clear_proxy),
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise SyncError(f"mc 命令失败：{shlex.join(command)}\n{detail}")
    return result


def remote_has_objects(remote: str, *, clear_proxy: bool, dry_run: bool) -> bool:
    result = run_mc(
        ["ls", "--recursive", "--json", remote],
        clear_proxy=clear_proxy,
        dry_run=dry_run,
        check=False,
    )
    if dry_run:
        return False
    if result.returncode == 0:
        return bool(result.stdout.strip())
    detail = (result.stderr or result.stdout).casefold()
    missing_markers = ("not found", "does not exist", "unable to stat", "specified key does not exist")
    if any(marker in detail for marker in missing_markers):
        return False
    raise SyncError((result.stderr or result.stdout).strip() or f"无法访问 remote：{remote}")


def mirror(
    source: str | Path,
    target: str | Path,
    *,
    clear_proxy: bool,
    dry_run: bool,
    remove: bool = False,
) -> None:
    arguments = ["mirror", "--quiet", "--overwrite"]
    if remove:
        arguments.append("--remove")
    arguments.extend((str(source), str(target)))
    run_mc(arguments, clear_proxy=clear_proxy, dry_run=dry_run)


def copy_object(
    source: str | Path,
    target: str | Path,
    *,
    clear_proxy: bool,
    dry_run: bool,
) -> None:
    if not dry_run and isinstance(target, Path):
        target.parent.mkdir(parents=True, exist_ok=True)
    run_mc(
        ["cp", "--quiet", str(source), str(target)],
        clear_proxy=clear_proxy,
        dry_run=dry_run,
    )


def validate_control_plane(root: Path) -> None:
    required = ("config.template.json", "scripts/memory.py", "scripts/sync.py")
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise SyncError(
            "public Git core 不完整；请先 clone 或更新仓库。缺少：" + ", ".join(missing)
        )


def validate_local_memory(root: Path) -> None:
    script = root / "scripts" / "memory.py"
    result = subprocess.run(
        [sys.executable, str(script), "--root", str(root), "validate"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SyncError((result.stderr or result.stdout).strip())


def validate_daily_files(root: Path) -> None:
    seen: set[str] = set()
    for path in root.glob("*/*/*.json"):
        try:
            document = memory_store.read_daily(path)
        except (ValueError, OSError) as exc:
            raise SyncError(str(exc)) from exc
        for event in document["events"]:
            if event["event_id"] in seen:
                raise SyncError(f"raw memory 包含重复 event_id：{path}")
            seen.add(event["event_id"])


@contextmanager
def shared_lock(root: Path) -> Iterator[None]:
    path = root / ".local" / ".locks" / "shared-sync.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def push_memory(root: Path, config: dict[str, Any], *, dry_run: bool) -> None:
    machine_id = str(config["machine_id"])
    source = root / ".share" / "memory" / machine_id
    sync = config["sync"]
    if not any(source.glob("*/*.json")):
        print(f"当前机器还没有 raw memory：{machine_id}")
        return
    target = remote_path(sync["remote"], "memory", machine_id)
    if dry_run:
        mirror(source, target, clear_proxy=bool(sync.get("clear_proxy")), dry_run=True)
        return
    # Serialize push/pull on this host; append can continue while the snapshot uploads.
    with memory_store.operation_lock(root, "raw-sync"):
        with tempfile.TemporaryDirectory(prefix="agent-memory-push-") as temporary:
            snapshot = Path(temporary) / machine_id
            with memory_store.operation_lock(root, "memory-write"):
                for path in source.glob("*/*.json"):
                    document = memory_store.read_daily(path)
                    atomic_write_json(snapshot / path.relative_to(source), document)
            validate_daily_files(Path(temporary))
            mirror(snapshot, target, clear_proxy=bool(sync.get("clear_proxy")), dry_run=False)
    print(f"已上传当前机器 memory：{machine_id}")


def push_all_memory(root: Path, config: dict[str, Any], *, dry_run: bool) -> None:
    source = root / ".share" / "memory"
    sync = config["sync"]
    target = remote_path(sync["remote"], "memory")
    if dry_run:
        mirror(source, target, clear_proxy=bool(sync.get("clear_proxy")), dry_run=True)
        return
    with memory_store.operation_lock(root, "raw-sync"):
        with tempfile.TemporaryDirectory(prefix="agent-memory-init-") as temporary:
            snapshot = Path(temporary)
            with memory_store.operation_lock(root, "memory-write"):
                for path in source.glob("*/*/*.json"):
                    atomic_write_json(snapshot / path.relative_to(source), memory_store.read_daily(path))
            validate_daily_files(snapshot)
            mirror(snapshot, target, clear_proxy=bool(sync.get("clear_proxy")), dry_run=False)
    print("已上传初始化 memory 基线")


def pull_other_memory(root: Path, config: dict[str, Any], *, dry_run: bool, include_owned: bool = False) -> None:
    with memory_store.operation_lock(root, "raw-sync"):
        _pull_other_memory(root, config, dry_run=dry_run, include_owned=include_owned)


def _pull_other_memory(root: Path, config: dict[str, Any], *, dry_run: bool, include_owned: bool) -> None:
    sync = config["sync"]
    remote = remote_path(sync["remote"], "memory")
    clear_proxy = bool(sync.get("clear_proxy"))
    if not remote_has_objects(remote, clear_proxy=clear_proxy, dry_run=dry_run):
        print("远端还没有 raw memory")
        return
    if dry_run:
        mirror(remote, "<temporary-memory-staging>", clear_proxy=clear_proxy, dry_run=True)
        print(f"DRY-RUN: merge all machines except {config['machine_id']}")
        return
    with tempfile.TemporaryDirectory(prefix="agent-memory-pull-") as temporary:
        staging = Path(temporary) / "memory"
        staging.mkdir()
        mirror(remote, staging, clear_proxy=clear_proxy, dry_run=False)
        destination = root / ".share" / "memory"
        # Ignore our own (possibly stale) remote prefix, including its validation.
        if not include_owned:
            shutil.rmtree(staging / str(config["machine_id"]), ignore_errors=True)
        validate_daily_files(staging)
        with memory_store.operation_lock(root, "memory-write"):
            local_events, errors = memory_store.load_events(root)
            if errors:
                raise SyncError("本地记忆校验失败，请先运行 memory.py validate")
            local = {event["event_id"]: event for event in local_events}
            incoming: dict[str, dict[str, Any]] = {}
            documents = []
            for path in sorted(staging.glob("*/*/*.json")):
                document = memory_store.read_daily(path)
                documents.append((destination / path.relative_to(staging), document))
                for raw in document["events"]:
                    event = memory_store.normalize_event(raw)
                    incoming[event["event_id"]] = event
            missing = sorted(
                key for key, event in local.items()
                if (include_owned or event["machine_id"] != config["machine_id"])
                and key not in incoming
            )
            changed = sorted(
                key for key in local.keys() & incoming.keys() if local[key] != incoming[key]
            )
            if missing or changed:
                report = root / ".local" / ".sync" / "raw-conflict.json"
                atomic_write_json(report, {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "missing_ids": missing, "changed_ids": changed,
                })
                raise SyncError(
                    f"远端 raw memory 回退或改写（缺失 {len(missing)}，冲突 {len(changed)}），"
                    f"未安装；请核验 {report}。脱敏改写也需人工确认，不自动合并。"
                )
            for target, document in documents:
                if not target.exists() or memory_store.read_daily(target) != document:
                    atomic_write_json(target, document)
            (root / ".local" / ".sync" / "raw-conflict.json").unlink(missing_ok=True)
    if include_owned:
        print(f"已恢复全部 memory，包含本机目录 {config['machine_id']}")
    else:
        print(f"已拉取其他机器 memory，本机目录 {config['machine_id']} 未被覆盖")


def copy_shared_snapshot(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in SHARED_DIRS:
        path = source / ".share" / name
        if path.is_dir():
            shutil.copytree(path, destination / ".share" / name, dirs_exist_ok=True, ignore=COPY_IGNORE)


def shared_manifest(snapshot: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for name in SHARED_DIRS:
        path = snapshot / ".share" / name
        if path.is_dir():
            for file_path in sorted(path.rglob("*")):
                if not file_path.is_file():
                    continue
                relative = file_path.relative_to(snapshot).as_posix()
                manifest[relative] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return manifest


def changed_paths(base: dict[str, str], side: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in set(base) | set(side)
        if base.get(path) != side.get(path)
    )


def shared_base(root: Path) -> Path:
    return root / ".local" / ".sync" / "base"


def shared_conflict(root: Path) -> Path:
    return root / ".local" / ".sync" / "conflict"


def replace_directory(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.sync")
    shutil.rmtree(temporary, ignore_errors=True)
    shutil.copytree(source, temporary, ignore=COPY_IGNORE)
    shutil.rmtree(target, ignore_errors=True)
    os.replace(temporary, target)


def install_shared_snapshot(root: Path, snapshot: Path) -> None:
    for name in SHARED_DIRS:
        source = snapshot / ".share" / name
        target = root / ".share" / name
        if source.is_dir():
            replace_directory(source, target)
        else:
            shutil.rmtree(target, ignore_errors=True)


def update_shared_base(root: Path, snapshot: Path) -> None:
    base = shared_base(root)
    temporary = base.with_name(f".{base.name}.{uuid.uuid4().hex}.sync")
    shutil.rmtree(temporary, ignore_errors=True)
    copy_shared_snapshot(snapshot, temporary)
    shutil.rmtree(base, ignore_errors=True)
    base.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, base)


def clear_shared_conflict(root: Path) -> None:
    shutil.rmtree(shared_conflict(root), ignore_errors=True)


def save_shared_conflict(
    root: Path,
    *,
    remote_snapshot: Path,
    base_manifest: dict[str, str],
    local_manifest: dict[str, str],
    remote_manifest: dict[str, str],
) -> Path:
    conflict = shared_conflict(root)
    temporary = conflict.with_name(f".{conflict.name}.{uuid.uuid4().hex}.sync")
    shutil.rmtree(temporary, ignore_errors=True)
    copy_shared_snapshot(remote_snapshot, temporary / "remote")
    base = shared_base(root)
    if base.is_dir():
        copy_shared_snapshot(base, temporary / "base")
    atomic_write_json(
        temporary / "report.json",
        {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "base_manifest": base_manifest,
            "local_manifest": local_manifest,
            "remote_manifest": remote_manifest,
            "local_changes": changed_paths(base_manifest, local_manifest),
            "remote_changes": changed_paths(base_manifest, remote_manifest),
        },
    )
    shutil.rmtree(conflict, ignore_errors=True)
    conflict.parent.mkdir(parents=True, exist_ok=True)
    os.replace(temporary, conflict)
    return conflict


def download_shared(
    remote: str,
    destination: Path,
    *,
    clear_proxy: bool,
    dry_run: bool,
) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in SHARED_DIRS:
        target = destination / ".share" / name
        if not dry_run:
            target.mkdir(parents=True, exist_ok=True)
        mirror(
            remote_path(remote, name),
            target,
            clear_proxy=clear_proxy,
            dry_run=dry_run,
        )


def upload_shared(
    root: Path,
    config: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    sync = config["sync"]
    remote = sync["remote"]
    clear_proxy = bool(sync.get("clear_proxy"))
    with tempfile.TemporaryDirectory(prefix="agent-shared-upload-") as temporary:
        snapshot = Path(temporary) / "shared"
        copy_shared_snapshot(root, snapshot)
        missing = [
            name
            for name in SHARED_DIRS
            if not (snapshot / ".share" / name).is_dir()
        ]
        if missing:
            raise SyncError("本地 shared 内容不完整：" + ", ".join(missing))
        for name in SHARED_DIRS:
            mirror(
                snapshot / ".share" / name,
                remote_path(remote, name),
                clear_proxy=clear_proxy,
                dry_run=dry_run,
                remove=True,
            )


def normalize_shared_file(root: Path, value: Path) -> tuple[Path, str]:
    """Return a local shared file and its snapshot-relative path."""
    candidate = value.expanduser()
    if not candidate.is_absolute():
        parts = candidate.parts
        candidate = root / candidate if parts and parts[0] == ".share" else root / ".share" / candidate
    candidate = candidate.resolve()
    shared = (root / ".share").resolve()
    try:
        candidate.relative_to(shared)
    except ValueError as exc:
        raise SyncError(f"只能发布 .agents/.share 下的文件：{candidate}") from exc
    if not candidate.is_file():
        raise SyncError(f"待发布 shared 文件不存在：{candidate}")
    relative = candidate.relative_to(root.resolve()).as_posix()
    if relative.split("/", 2)[1] not in SHARED_DIRS:
        raise SyncError(f"不支持的 shared 文件路径：{relative}")
    return candidate, relative


def publish_shared_file(
    root: Path,
    config: dict[str, Any],
    value: Path,
    *,
    dry_run: bool,
    allow_non_writer: bool,
) -> None:
    """Publish one evaluator-owned shared file without overwriting other writers.

    Remote changes to every other shared file are pulled into the local snapshot.
    A concurrent change to the same file, or unrelated uncommitted local shared
    changes, is rejected instead of being silently overwritten.
    """
    require_shared_writer(config, allow_non_writer=allow_non_writer)
    local_file, relative = normalize_shared_file(root, value)
    sync = config["sync"]
    clear_proxy = bool(sync.get("clear_proxy"))
    if dry_run:
        print(f"DRY-RUN: publish only {relative} and pull other remote shared changes")
        return

    with shared_lock(root), tempfile.TemporaryDirectory(prefix="agent-shared-file-") as temporary:
        temporary_root = Path(temporary)
        local_snapshot = temporary_root / "local"
        remote_snapshot = temporary_root / "remote"
        merged_snapshot = temporary_root / "merged"
        copy_shared_snapshot(root, local_snapshot)
        download_shared(
            sync["remote"],
            remote_snapshot,
            clear_proxy=clear_proxy,
            dry_run=False,
        )

        base_path = shared_base(root)
        if not base_path.is_dir():
            raise SyncError("shared 尚无同步基线；请先运行 sync-shared")
        base = shared_manifest(base_path)
        local = shared_manifest(local_snapshot)
        remote = shared_manifest(remote_snapshot)
        unrelated_local = [path for path in changed_paths(base, local) if path != relative]
        if unrelated_local:
            raise SyncError(
                "本地还有其他未发布 shared 修改，拒绝文件级发布："
                + ", ".join(unrelated_local)
            )
        if base.get(relative) != remote.get(relative) and local.get(relative) != remote.get(relative):
            conflict = save_shared_conflict(
                root,
                remote_snapshot=remote_snapshot,
                base_manifest=base,
                local_manifest=local,
                remote_manifest=remote,
            )
            raise SyncError(
                f"远端同一 shared 文件已并发修改：{relative}；请合并 {conflict / 'remote'}"
            )

        # H1 加固（2026-08-12 审计）：远端缺少 base 中已有文件（旧版覆盖/删除
        # 特征）时拒绝合并——merged 以 remote 为底，缺文件会被 install 覆盖掉本地。
        missing_in_remote = sorted(set(base) - set(remote))
        if missing_in_remote:
            conflict = save_shared_conflict(
                root,
                remote_snapshot=remote_snapshot,
                base_manifest=base,
                local_manifest=local,
                remote_manifest=remote,
            )
            preview = ", ".join(missing_in_remote[:10])
            if len(missing_in_remote) > 10:
                preview += f" 等 {len(missing_in_remote)} 个"
            raise SyncError(
                "远端缺少本地已有的 shared 文件（疑似远端被旧版覆盖/删除）："
                f"{preview}；已保存远端快照 {conflict / 'remote'}，"
                "请人工确认（正常删除则接受）后运行 resolve-shared"
            )
        copy_shared_snapshot(remote_snapshot, merged_snapshot)
        merged_file = merged_snapshot / relative
        merged_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_file, merged_file)
        if local.get(relative) != remote.get(relative):
            copy_object(
                local_file,
                remote_path(sync["remote"], relative.removeprefix(".share/")),
                clear_proxy=clear_proxy,
                dry_run=False,
            )
        install_shared_snapshot(root, merged_snapshot)
        update_shared_base(root, merged_snapshot)
        clear_shared_conflict(root)
    print(f"已发布 shared 文件：{relative}")


def reconcile_shared(
    root: Path,
    config: dict[str, Any],
    *,
    mode: str,
    dry_run: bool,
    allow_non_writer: bool,
) -> None:
    if mode not in {"pull", "push", "both"}:
        raise ValueError(f"unsupported shared mode: {mode}")
    sync = config["sync"]
    clear_proxy = bool(sync.get("clear_proxy"))
    can_push = is_shared_writer(config) or allow_non_writer

    if dry_run:
        download_shared(
            sync["remote"],
            Path("<temporary-shared-staging>"),
            clear_proxy=clear_proxy,
            dry_run=True,
        )
        print(f"DRY-RUN: compare local/shared/remote baseline; mode={mode}")
        return

    with shared_lock(root), tempfile.TemporaryDirectory(prefix="agent-shared-sync-") as temporary:
        temporary_root = Path(temporary)
        local_snapshot = temporary_root / "local"
        remote_snapshot = temporary_root / "remote"
        copy_shared_snapshot(root, local_snapshot)
        download_shared(
            sync["remote"],
            remote_snapshot,
            clear_proxy=clear_proxy,
            dry_run=False,
        )

        local = shared_manifest(local_snapshot)
        remote = shared_manifest(remote_snapshot)
        base_path = shared_base(root)
        has_base = base_path.is_dir()
        base = shared_manifest(base_path) if has_base else {}

        if local == remote:
            update_shared_base(root, local_snapshot)
            clear_shared_conflict(root)
            print("Shared 内容已对齐")
            return

        local_changed = local != base
        remote_changed = remote != base

        if not has_base:
            if not local:
                install_shared_snapshot(root, remote_snapshot)
                update_shared_base(root, remote_snapshot)
                clear_shared_conflict(root)
                print("已首次拉取 S3 shared 内容")
                return
            if not remote and mode in {"push", "both"} and can_push:
                upload_shared(root, config, dry_run=False)
                update_shared_base(root, local_snapshot)
                clear_shared_conflict(root)
                print("已首次上传 shared 内容")
                return
            conflict = save_shared_conflict(
                root,
                remote_snapshot=remote_snapshot,
                base_manifest=base,
                local_manifest=local,
                remote_manifest=remote,
            )
            raise SyncError(
                "本地与 S3 shared 尚无共同基线且内容不同；请先合并 "
                f"{conflict / 'remote'} 到本地，再运行 resolve-shared"
            )

        if local_changed and remote_changed:
            conflict = save_shared_conflict(
                root,
                remote_snapshot=remote_snapshot,
                base_manifest=base,
                local_manifest=local,
                remote_manifest=remote,
            )
            raise SyncError(
                "检测到 shared 分叉，已保留本地并保存远端快照。"
                f"请合并 {conflict / 'remote'}，再运行 resolve-shared"
            )

        if remote_changed:
            # 2026-08-12 加固（13:13 事故）：远端缺少本地（==基线）已有的
            # 文件，疑似远端被旧版覆盖或删除——拒绝无条件拉取，保存远端快照
            # 人工确认（正常删除也走此路径，确认后 resolve-shared 接受删除）。
            missing_in_remote = sorted(set(base) - set(remote))
            if missing_in_remote:
                conflict = save_shared_conflict(
                    root,
                    remote_snapshot=remote_snapshot,
                    base_manifest=base,
                    local_manifest=local,
                    remote_manifest=remote,
                )
                preview = ", ".join(missing_in_remote[:10])
                if len(missing_in_remote) > 10:
                    preview += f" 等 {len(missing_in_remote)} 个"
                raise SyncError(
                    "远端缺少本地已有的 shared 文件（疑似远端被旧版覆盖/删除）："
                    f"{preview}；已保存远端快照 {conflict / 'remote'}，"
                    "请人工确认（正常删除则接受）后运行 resolve-shared"
                )
            install_shared_snapshot(root, remote_snapshot)
            update_shared_base(root, remote_snapshot)
            clear_shared_conflict(root)
            print("已拉取 S3 shared 更新")
            return

        if local_changed:
            if mode == "pull":
                print("本地 shared 有未上传修改；pull 未覆盖本地")
                return
            if not can_push:
                raise SyncError("本地 shared 有修改，但当前机器不是 shared_writer")
            upload_shared(root, config, dry_run=False)
            update_shared_base(root, local_snapshot)
            clear_shared_conflict(root)
            print("已上传本地 shared 更新")
            return

        raise SyncError("shared 状态无法归类，请检查 .sync/base 和 S3 内容")


def resolve_shared(
    root: Path,
    config: dict[str, Any],
    *,
    dry_run: bool,
    allow_non_writer: bool,
) -> None:
    require_shared_writer(config, allow_non_writer=allow_non_writer)
    conflict = shared_conflict(root)
    report_path = conflict / "report.json"
    if not report_path.is_file():
        raise SyncError("没有待处理的 shared 分叉")
    report = read_json(report_path)

    if dry_run:
        print("DRY-RUN: verify remote conflict snapshot, upload merged local shared, refresh baseline")
        return

    sync = config["sync"]
    clear_proxy = bool(sync.get("clear_proxy"))
    with shared_lock(root), tempfile.TemporaryDirectory(prefix="agent-shared-resolve-") as temporary:
        remote_snapshot = Path(temporary) / "remote"
        download_shared(
            sync["remote"],
            remote_snapshot,
            clear_proxy=clear_proxy,
            dry_run=False,
        )
        if shared_manifest(remote_snapshot) != report.get("remote_manifest"):
            raise SyncError("S3 shared 在合并期间再次变化；请重新运行 sync-shared 获取最新分叉")
        # H2 加固（2026-08-12 审计）：resolve 是整包上传，若用户只合并了部分
        # 冲突文件，远端新增（base 没有而远端有）未被并入本地——整包上传会把
        # 远端真实更新覆盖回旧版。上传前确认远端新增均已进入本地。
        base_manifest = report.get("base_manifest", {})
        remote_manifest = shared_manifest(remote_snapshot)
        local_manifest = shared_manifest(root)
        unmerged = sorted(set(remote_manifest) - set(base_manifest) - set(local_manifest))
        if unmerged:
            preview = ", ".join(unmerged[:10])
            if len(unmerged) > 10:
                preview += f" 等 {len(unmerged)} 个"
            raise SyncError(
                "resolve 前请先合并远端新增文件到本地（当前缺失）："
                f"{preview}；请从 {conflict / 'remote'} 补齐后再 resolve-shared"
            )
        upload_shared(root, config, dry_run=False)
        local_snapshot = Path(temporary) / "local"
        copy_shared_snapshot(root, local_snapshot)
        update_shared_base(root, local_snapshot)
        clear_shared_conflict(root)
    print("已上传合并后的 shared 内容并刷新基线")


def ensure_link(path: Path, target: Path, *, dry_run: bool) -> None:
    if path.is_symlink() and path.resolve() == target.resolve():
        return
    if path.exists() or path.is_symlink():
        raise SyncError(f"不会覆盖已有 Agent 配置：{path}")
    if dry_run:
        print(f"DRY-RUN: ln -s {target} {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target, target_is_directory=target.is_dir())


def configure_agent_links(root: Path, config: dict[str, Any], *, dry_run: bool, strict: bool = True) -> None:
    skills_root = root / ".share" / "skills"
    # 2026-08-12 skills 按 common/eval/memory/train 分类后为多级布局：
    # 递归发现所有含 SKILL.md 的目录（父目录即 skill 名），跳过分类目录本身。
    skills = sorted(
        skill_path.parent
        for skill_path in skills_root.rglob("SKILL.md")
        if skill_path.parent != skills_root and skill_path.parent.is_dir()
    )
    # Old layouts leave a copy in a category named after its nested skill.
    # Retain remote files for old clients; only link the canonical nested entry.
    skills = [
        skill for skill in skills
        if not any(skill in child.parents and skill.name == child.name for child in skills)
    ]
    names = [skill.name for skill in skills]
    if len(set(names)) != len(names):
        raise SyncError("多个不同分类中有同名 Skill，请明确唯一来源后再建立链接")
    if not skills:
        raise SyncError(f"共享 Skill 目录为空：{skills_root}")
    links: list[tuple[Path, Path]] = []
    for skill in skills:
        for agent_home in (".codex", ".claude", ".kimi-code"):
            links.append((Path.home() / agent_home / "skills" / skill.name, skill))
    workspace = Path(os.path.expanduser(str(config["workspace_root"])))
    links.append((workspace / "AGENTS.md", root / ".share" / "config" / "prompts" / "AGENTS.md"))
    links.append((workspace / "CLAUDE.md", root / ".share" / "config" / "prompts" / "CLAUDE.md"))
    for path, target in links:
        try:
            ensure_link(path, target, dry_run=dry_run)
        except SyncError as error:
            # strict=False（sync/pull 路径）：单条链接冲突不阻断同步，
            # 机器上已有同名真实配置时保留现状并提示。
            if strict:
                raise
            print(f"[links] {error}", file=sys.stderr, flush=True)


def build_local_config(
    template: dict[str, Any],
    *,
    remote: str,
    machine_id: str,
    machine_role: str,
    workspace_root: str,
    shared_writer: bool,
    clear_proxy: bool,
) -> dict[str, Any]:
    if not SAFE_NAME.fullmatch(machine_id):
        raise SyncError("machine_id 只能包含字母、数字、点、下划线和连字符")
    config = json.loads(json.dumps(template))
    config["machine_id"] = machine_id
    config["machine_role"] = machine_role
    config["workspace_root"] = workspace_root
    config["shared_writer"] = shared_writer
    config["long_term_writer"] = shared_writer
    config["sync"] = {
        "enabled": True,
        "initialized": True,
        "remote": validate_remote(remote),
        "clear_proxy": clear_proxy,
    }
    return config


def command_init(root: Path, args: argparse.Namespace) -> int:
    validate_control_plane(root)
    if args.push:
        config = load_config(root, require_initialized=False)
        require_shared_writer(config, allow_non_writer=args.allow_non_writer)
        validate_local_memory(root)
        sync = config["sync"]
        memory_remote = remote_path(sync["remote"], "memory")
        if remote_has_objects(
            memory_remote,
            clear_proxy=bool(sync.get("clear_proxy")),
            dry_run=args.dry_run,
        ) and not args.force:
            raise SyncError("远端 memory 不是空目录；确认后可使用 --force")
        push_all_memory(root, config, dry_run=args.dry_run)
        upload_shared(root, config, dry_run=args.dry_run)
        if not args.dry_run:
            with tempfile.TemporaryDirectory(prefix="agent-shared-base-") as temporary:
                snapshot = Path(temporary) / "local"
                copy_shared_snapshot(root, snapshot)
                update_shared_base(root, snapshot)
            config["sync"]["initialized"] = True
            atomic_write_json(root / "config.json", config)
        print(f"S3 memory/shared 初始化完成：{sync['remote']}")
        return 0

    required = {
        "--remote": args.remote,
        "--machine-id": args.machine_id,
        "--machine-role": args.machine_role,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SyncError(f"init --pull 缺少参数：{', '.join(missing)}")
    if (root / "config.json").exists() and not args.force:
        raise SyncError(f"本机已经存在 config.json：{root / 'config.json'}")

    remote = validate_remote(args.remote)
    clear_proxy = bool(args.clear_proxy)
    machine_remote = remote_path(remote, "memory", args.machine_id)
    if remote_has_objects(
        machine_remote,
        clear_proxy=clear_proxy,
        dry_run=args.dry_run,
    ) and not args.reuse_machine:
        raise SyncError("远端已存在同名 machine memory；请更换 machine ID")

    if args.dry_run:
        download_shared(
            remote,
            Path("<temporary-shared-staging>"),
            clear_proxy=clear_proxy,
            dry_run=True,
        )
        print("DRY-RUN: install shared, generate config.json, pull raw memory, create links")
        return 0

    with shared_lock(root), tempfile.TemporaryDirectory(prefix="agent-shared-init-") as temporary:
        shared_snapshot = Path(temporary) / "shared"
        download_shared(remote, shared_snapshot, clear_proxy=clear_proxy, dry_run=False)
        install_shared_snapshot(root, shared_snapshot)
        update_shared_base(root, shared_snapshot)

    template = read_json(root / "config.template.json")
    config = build_local_config(
        template,
        remote=remote,
        machine_id=args.machine_id,
        machine_role=args.machine_role,
        workspace_root=args.workspace_root,
        shared_writer=args.shared_writer,
        clear_proxy=clear_proxy,
    )
    pull_other_memory(root, config, dry_run=False, include_owned=args.reuse_machine)
    atomic_write_json(root / "config.json", config)
    configure_agent_links(root, config, dry_run=False)
    validate_local_memory(root)
    print(f"新机器初始化完成：{args.machine_id}")
    return 0


def command_pull(root: Path, args: argparse.Namespace) -> int:
    config = load_config(root)
    pull_other_memory(root, config, dry_run=args.dry_run)
    reconcile_shared(
        root,
        config,
        mode="pull",
        dry_run=args.dry_run,
        allow_non_writer=False,
    )
    # 拉取后为新增 Skill 建立 agent 链接（已有同名真实配置则警告跳过）
    configure_agent_links(root, config, dry_run=args.dry_run, strict=False)
    if not args.dry_run:
        validate_local_memory(root)
    return 0


def command_push_memory(root: Path, args: argparse.Namespace) -> int:
    config = load_config(root)
    validate_local_memory(root)
    push_memory(root, config, dry_run=args.dry_run)
    return 0


def command_push(root: Path, args: argparse.Namespace) -> int:
    config = load_config(root)
    validate_local_memory(root)
    push_memory(root, config, dry_run=args.dry_run)
    reconcile_shared(
        root,
        config,
        mode="push",
        dry_run=args.dry_run,
        allow_non_writer=args.allow_non_writer,
    )
    return 0


def command_sync(root: Path, args: argparse.Namespace) -> int:
    config = load_config(root)
    validate_local_memory(root)
    push_memory(root, config, dry_run=args.dry_run)
    pull_other_memory(root, config, dry_run=args.dry_run)
    reconcile_shared(
        root,
        config,
        mode="both",
        dry_run=args.dry_run,
        allow_non_writer=False,
    )
    # 同步后为新增 Skill 建立 agent 链接（已有同名真实配置则警告跳过）
    configure_agent_links(root, config, dry_run=args.dry_run, strict=False)
    if not args.dry_run:
        validate_local_memory(root)
    return 0


def command_pull_shared(root: Path, args: argparse.Namespace) -> int:
    config = load_config(root)
    reconcile_shared(
        root,
        config,
        mode="pull",
        dry_run=args.dry_run,
        allow_non_writer=False,
    )
    configure_agent_links(root, config, dry_run=args.dry_run, strict=False)
    return 0


def command_push_shared(root: Path, args: argparse.Namespace) -> int:
    config = load_config(root)
    reconcile_shared(
        root,
        config,
        mode="push",
        dry_run=args.dry_run,
        allow_non_writer=args.allow_non_writer,
    )
    return 0


def command_publish_shared_file(root: Path, args: argparse.Namespace) -> int:
    config = load_config(root)
    publish_shared_file(
        root,
        config,
        args.path,
        dry_run=args.dry_run,
        allow_non_writer=args.allow_non_writer,
    )
    return 0


def command_sync_shared(root: Path, args: argparse.Namespace) -> int:
    config = load_config(root)
    reconcile_shared(
        root,
        config,
        mode="both",
        dry_run=args.dry_run,
        allow_non_writer=args.allow_non_writer,
    )
    return 0


def command_resolve_shared(root: Path, args: argparse.Namespace) -> int:
    config = load_config(root)
    resolve_shared(
        root,
        config,
        dry_run=args.dry_run,
        allow_non_writer=args.allow_non_writer,
    )
    return 0


def command_status(root: Path, args: argparse.Namespace) -> int:
    config = load_config(root, require_initialized=False)
    files = list((root / ".share" / "memory").glob("*/*/*.json"))
    event_count = 0
    machines: set[str] = set()
    for path in files:
        document = read_json(path)
        event_count += len(document.get("events", []))
        machines.add(path.parts[-3])
    sync = config["sync"]
    print(f"Machine: {config['machine_id']} ({config.get('machine_role', '-')})")
    print(f"Shared writer: {is_shared_writer(config)}")
    print(f"Sync initialized: {bool(sync.get('initialized'))}")
    print(f"Remote: {sync['remote']}")
    print(f"Local memory: {len(files)} files, {event_count} events, {len(machines)} machines")
    print(f"Shared baseline: {shared_base(root).is_dir()}")
    print(f"Shared conflict: {shared_conflict(root).is_dir()}")
    if args.remote_check:
        populated = remote_has_objects(
            sync["remote"],
            clear_proxy=bool(sync.get("clear_proxy")),
            dry_run=args.dry_run,
        )
        print(f"Remote initialized: {populated}")
    return 0


def add_writer_override(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-non-writer",
        action="store_true",
        help="仅用于用户明确授权的维护迁移；不跳过分叉检查",
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="S3-backed workspace memory synchronization")
    result.add_argument("--root", type=Path, default=ROOT, help=".agents 目录")
    result.add_argument("--dry-run", action="store_true")
    subparsers = result.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="初始化 S3 或新机器")
    mode = init.add_mutually_exclusive_group(required=True)
    mode.add_argument("--push", action="store_true", help="从当前机器初始化 S3")
    mode.add_argument("--pull", action="store_true", help="从 S3 初始化新机器")
    init.add_argument("--remote")
    init.add_argument("--machine-id")
    init.add_argument("--machine-role")
    init.add_argument("--workspace-root", default="~/code")
    init.add_argument("--shared-writer", action="store_true")
    init.add_argument("--clear-proxy", action=argparse.BooleanOptionalAction, default=True)
    init.add_argument("--reuse-machine", action="store_true")
    init.add_argument("--force", action="store_true")
    add_writer_override(init)
    init.set_defaults(func=command_init)

    pull = subparsers.add_parser("pull", help="拉取 raw memory 和 shared")
    pull.set_defaults(func=command_pull)

    push = subparsers.add_parser("push", help="上传 raw memory，并安全同步 shared")
    add_writer_override(push)
    push.set_defaults(func=command_push)

    push_memory_parser = subparsers.add_parser("push-memory", help="直接 mirror 当前机器 raw memory")
    push_memory_parser.set_defaults(func=command_push_memory)

    sync = subparsers.add_parser("sync", help="同步 raw memory 和 shared")
    sync.set_defaults(func=command_sync)

    pull_shared = subparsers.add_parser("pull-shared", help="安全拉取 Skill、long-term 和共享配置")
    pull_shared.set_defaults(func=command_pull_shared)

    push_shared = subparsers.add_parser("push-shared", help="检查分叉后上传 shared")
    add_writer_override(push_shared)
    push_shared.set_defaults(func=command_push_shared)

    publish_file = subparsers.add_parser(
        "publish-shared-file",
        help="只发布一个有明确所有权的 shared 文件，并拉取其余远端更新",
    )
    publish_file.add_argument("path", type=Path)
    add_writer_override(publish_file)
    publish_file.set_defaults(func=command_publish_shared_file)

    sync_shared = subparsers.add_parser("sync-shared", help="双向协调 shared；分叉时拒绝覆盖")
    add_writer_override(sync_shared)
    sync_shared.set_defaults(func=command_sync_shared)

    resolve = subparsers.add_parser("resolve-shared", help="上传已人工合并的 shared 分叉")
    add_writer_override(resolve)
    resolve.set_defaults(func=command_resolve_shared)

    status = subparsers.add_parser("status", help="显示本地和可选远端状态")
    status.add_argument("--remote-check", action="store_true")
    status.set_defaults(func=command_status)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        return args.func(args.root.resolve(), args)
    except (OSError, SyncError) as exc:
        print(f"sync: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
