import json
import os
import subprocess
import sys

import pytest

from tools.claude_feishu.__main__ import settings_for_profile
from tools.config import ROOT


def test_default_profile_stays_readonly():
    profile, path = settings_for_profile({})
    assert profile == "read-only"
    permissions = json.loads(path.read_text())["permissions"]
    assert {"Bash", "Edit", "Write"}.issubset(permissions["deny"])


def test_connection_readiness_does_not_log_signed_url(monkeypatch, tmp_path, caplog):
    import asyncio
    import importlib.util
    import logging
    from unittest.mock import AsyncMock, patch

    monkeypatch.setenv("CLAUDE_FEISHU_STATE", str(tmp_path / "seen.json"))
    monkeypatch.setenv("CLAUDE_FEISHU_SESSIONS", str(tmp_path / "sessions.json"))
    spec = importlib.util.spec_from_file_location(
        "ready_bridge", ROOT / "tools/claude_feishu/bridge.py"
    )
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    client = object.__new__(bridge.ObservedClient)
    client._conn = object()
    client._conn_url = "wss://example.invalid/SECRET"
    with patch.object(bridge.ws.Client, "_connect", new_callable=AsyncMock) as connect:
        caplog.set_level(logging.INFO)
        asyncio.run(client._connect())
        connect.assert_awaited_once()
    assert "Feishu websocket connected" in caplog.text
    assert "SECRET" not in caplog.text


def test_workspace_profile_preserves_permissions_and_denials():
    profile, path = settings_for_profile({"permission_profile": "workspace"})
    assert profile == "workspace"
    permissions = json.loads(path.read_text())["permissions"]
    assert set(permissions["allow"]) == {
        "Read",
        "Glob",
        "Grep",
        "Bash",
        "Edit",
        "Write",
        "WebFetch",
    }
    assert set(permissions["deny"]) == {
        "Bash(rm -rf *)",
        "Bash(rm -fr *)",
        "Bash(git push --force*)",
        "Bash(git reset --hard*)",
        "Bash(dd *)",
        "Bash(mkfs*)",
        "Bash(shutdown*)",
        "Bash(reboot*)",
    }


@pytest.mark.parametrize(
    "config",
    [
        {"permission_profile": "invalid"},
        {"permission_profile": []},
        {"settings": "arbitrary.json"},
    ],
)
def test_invalid_profile_fails_closed(config):
    with pytest.raises(ValueError):
        settings_for_profile(config)


@pytest.mark.parametrize("interval,progress_env,stream_env,expected", [
    (None, None, None, 5), (7, "11", "13", 7), (0, None, None, 0),
    (None, "11", "13", 11), (None, None, "13", 13),
])
def test_factory_applies_workspace_settings_without_launching_claude(tmp_path, interval, progress_env, stream_env, expected):
    credentials = tmp_path / "env"
    credentials.write_text(
        "FEISHU_APP_ID=test\nFEISHU_APP_SECRET=test\nFEISHU_ALLOW_OPEN_IDS=allowed\n"
    )
    credentials.chmod(0o600)
    state = tmp_path / "state"
    state.mkdir()
    (state / "sessions.json").write_text(
        json.dumps({"version": 1, "chats": {"chat": {"session_id": "test-id"}}})
    )
    config = {
        "permission_profile": "workspace",
        "credentials_file": str(credentials),
        "claude_cli": sys.executable,
        "workdir": str(tmp_path),
    }
    if interval is not None:
        config["progress_interval_seconds"] = interval
    script = """
import json,sys
import lark_oapi
from pathlib import Path
from unittest.mock import patch
from tools.claude_feishu.__main__ import factory
with patch("subprocess.Popen") as spawn:
    app = factory(json.loads(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    from tools.claude_feishu import bridge
    assert bridge.SETTINGS.endswith("claude-settings.workspace.json")
    assert bridge.WORKDIR == sys.argv[4]
    assert bridge.STREAM_INTERVAL == int(sys.argv[5])
    assert app.snapshot()["permission_profile"] == "workspace"
    spawn.assert_not_called()
    print("PROFILE_OK")
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            json.dumps(config),
            str(tmp_path / "config.json"),
            str(state),
            str(tmp_path),
            str(expected),
        ],
        cwd=ROOT,
        env={**{k: v for k, v in os.environ.items() if k not in (
            "CLAUDE_FEISHU_PROGRESS_INTERVAL", "CLAUDE_FEISHU_STREAM_INTERVAL")},
             **({"CLAUDE_FEISHU_PROGRESS_INTERVAL": progress_env} if progress_env is not None else {}),
             **({"CLAUDE_FEISHU_STREAM_INTERVAL": stream_env} if stream_env is not None else {}),
             "AGENTS_LOCAL_ROOT": str(tmp_path / "local")},
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "PROFILE_OK" in result.stdout
