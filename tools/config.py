"""Machine-local tool configuration. This module has no network side effects."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import stat


ROOT = Path(__file__).resolve().parents[1]


def local_root() -> Path:
    return (
        Path(os.environ.get("AGENTS_LOCAL_ROOT", ROOT / ".local"))
        .expanduser()
        .resolve()
    )


def workspace_root() -> Path:
    try:
        value = json.loads((ROOT / "config.json").read_text())["workspace_root"]
        return resolve_path(value, ROOT)
    except (OSError, ValueError, KeyError, TypeError):
        return ROOT.parent


def resolve_path(value: str | Path, base: Path) -> Path:
    path = Path(os.path.expandvars(str(value))).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def load_config(service: str, path: str | None) -> tuple[dict, Path]:
    source = (
        resolve_path(path, Path.cwd())
        if path
        else local_root() / "config" / f"{service}.json"
    )
    if not source.exists():
        if path:
            raise ValueError(f"config not found: {source}")
        return {}, source
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("tool config must be a JSON object")
    return value, source


def load_env_file(path: Path, *, required: bool = True) -> None:
    if not path.exists():
        if required:
            raise ValueError(f"credential file not found: {path}")
        return
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise ValueError(
            "credential file must be owned by the current user with mode 0600"
        )
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        words = shlex.split(line, comments=True)
        if words[:1] == ["export"]:
            words.pop(0)
        if not words:
            continue
        if len(words) != 1 or "=" not in words[0]:
            raise ValueError(
                f"credential file line {index}: expected a literal KEY=value assignment"
            )
        key, value = words[0].split("=", 1)
        if not key.replace("_", "").isalnum() or key[:1].isdigit():
            raise ValueError(f"credential file line {index}: invalid variable name")
        # Parse literal assignments only: never source or evaluate a shell file.
        os.environ[key] = value


def atomic_json(path: Path, value: object) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
