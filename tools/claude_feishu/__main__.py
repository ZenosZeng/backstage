"""Pixi-managed remote Claude bridge with machine-local credentials and state."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys

from tools.config import load_env_file, resolve_path, workspace_root
from tools.service import Application, main


def settings_for_profile(config: dict) -> tuple[str, Path]:
    profiles = {
        "read-only": "claude-settings.json",
        "workspace": "claude-settings.workspace.json",
    }
    profile = config.get("permission_profile", "read-only")
    if not isinstance(profile, str) or profile not in profiles:
        raise ValueError("permission_profile must be read-only or workspace")
    if config.get("settings"):
        raise ValueError(
            "use permission_profile to select the bundled permission settings"
        )
    settings = Path(__file__).with_name(profiles[profile])
    payload = json.loads(settings.read_text())
    if not isinstance(payload.get("permissions"), dict):
        raise ValueError("bundled Claude permission settings are invalid")
    return profile, settings


def factory(config: dict, config_path: Path, directory: Path) -> Application:
    credentials = resolve_path(
        config.get("credentials_file", "~/.config/claude-feishu/env"),
        config_path.parent,
    )
    load_env_file(credentials, required=bool(config.get("credentials_file")))
    if not os.environ.get("FEISHU_APP_ID") or not os.environ.get("FEISHU_APP_SECRET"):
        raise ValueError(
            "FEISHU_APP_ID and FEISHU_APP_SECRET are required in the private environment file"
        )
    if (
        not os.environ.get("FEISHU_ALLOW_OPEN_IDS")
        and os.environ.get("FEISHU_OPEN_MODE") != "1"
    ):
        raise ValueError("remote control requires an explicit user allowlist")
    workdir = resolve_path(config.get("workdir", workspace_root()), config_path.parent)
    if not workdir.is_dir():
        raise ValueError("remote workdir is not a directory")
    binary = str(
        config.get("claude_cli")
        or os.environ.get("CLAUDE_CLI")
        or shutil.which("claude")
        or ""
    )
    if not binary or not shutil.which(binary):
        raise ValueError(
            "Claude CLI not found; set claude_cli in the local configuration"
        )
    profile, settings = settings_for_profile(config)
    os.environ.update(
        {
            "CLAUDE_CLI": binary,
            "CLAUDE_FEISHU_WORKDIR": str(workdir),
            "CLAUDE_FEISHU_SETTINGS": str(settings),
            "CLAUDE_FEISHU_STATE": str(directory / "seen.json"),
            "CLAUDE_FEISHU_SESSIONS": str(directory / "sessions.json"),
            "CLAUDE_FEISHU_TIMEOUT": str(int(config.get("timeout_seconds", 600))),
        }
    )
    if int(os.environ["CLAUDE_FEISHU_TIMEOUT"]) <= 0:
        raise ValueError("timeout_seconds must be positive")
    from tools.claude_feishu import bridge

    def shutdown():
        bridge.shutdown()
        raise SystemExit(0)

    return Application(
        run=lambda stop: bridge.main(),
        snapshot=lambda: {
            "credentials_configured": True,
            "allowed_users": len(bridge.ALLOW_OPEN_IDS),
            "workdir": str(workdir),
            "permission_profile": profile,
            "session_count": len(bridge._chat_sessions),
        },
        shutdown=shutdown,
    )


if __name__ == "__main__":
    sys.exit(main("claude-feishu", ["tools.claude_feishu"], factory))
