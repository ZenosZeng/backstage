"""Replay captured DSH events and isolate all messaging/process integration."""

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.claude_feishu import harness


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_FEISHU_SESSIONS", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("CLAUDE_FEISHU_STATE", str(tmp_path / "seen.json"))
    monkeypatch.setenv("FEISHU_ALLOW_OPEN_IDS", "allowed")
    spec = importlib.util.spec_from_file_location(
        "bridge_dsh_test", ROOT / "tools/claude_feishu/bridge.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        harness,
        "REGISTRY",
        {
            "claude": harness.Harness("claude", sys.executable, "preregister"),
            "dsh": harness.Harness("dsh", sys.executable, "capture"),
        },
    )
    monkeypatch.setattr(module, "CLAUDE", sys.executable)
    monkeypatch.setattr(module, "_send_reply", mock.Mock(return_value=True))
    monkeypatch.setattr(module, "_post_card_reply", mock.Mock(return_value="card"))
    monkeypatch.setattr(module, "_patch_card", mock.Mock(return_value=True))
    monkeypatch.setattr(module, "_kick_queue", mock.Mock())
    return module


def events():
    return [
        json.loads(line)
        for line in Path(__file__)
        .with_name("fixtures")
        .joinpath("dsh_headless.jsonl")
        .read_text()
        .splitlines()
    ]


class TaskInput(io.StringIO):
    def close(self):
        self.task = self.getvalue()
        super().close()


class Proc:
    def __init__(self, stream, rc=0, err="", callback=None):
        self.returncode = None
        self.pid = 999999
        self.stdin = TaskInput()
        self.stderr = io.StringIO(err)
        self.rc = rc
        self.callback = callback
        self.stdout = self.lines(stream)

    def lines(self, stream):
        if self.callback:
            self.callback()
        for event in stream:
            yield json.dumps(event) + "\n"

    def wait(self, timeout=None):
        self.returncode = self.rc
        return self.rc

    def poll(self):
        return self.returncode


def test_captured_events_and_cards(bridge):
    state = bridge.RunState("dsh")
    for event in events():
        state.observe(event)
    assert state.session_id == "session-fixture"
    assert state.turns == 1 and state.step == 2
    assert state.total_usage["input_tokens"] == 5342
    assert state.total_usage["output_tokens"] == 155
    assert state.total_usage["cache_read_input_tokens"] == 5760
    assert state.context_used() == 5622  # last step only, not aggregate
    assert state.cost is None and not state.live_text
    assert "bash: ls -1" in state.steps
    assert "alpha.txt" in state.latest_output()
    assert "步骤 2" in state.progress_md(5)
    assert "ctx 6k" in state.statusline()
    assert "deepseek" not in state.statusline() and "%" not in state.statusline()
    state.returncode = 0
    card = bridge._card(state, 45, done=state.answer())
    assert card["header"]["title"]["content"] == "✅ DSH 执行完成 · 45s"
    assert "$" not in json.dumps(card)


def test_truncated_missing_fields_and_committed_text(bridge):
    state = bridge.RunState("dsh")
    state.observe({"type": "tool_call", "tool": "read", "truncated": True})
    assert "截断" in state.action
    state.observe({"type": "status", "phase": "step_end", "truncated": True})
    state.observe({"type": "status", "phase": "turn_end", "truncated": True})
    state.observe({"type": "text", "text": "第一行\n第二行"})
    assert state.latest_output() == "第一行\n第二行"
    assert "用量可能不完整" in state.progress_md(1)
    state.returncode = 1
    state.observe({"type": "final", "text": ""})
    assert bridge._card(state, 1, done="failed")["header"]["template"] == "red"
    assert "rc=1" in bridge._card(state, 1, done="failed")["header"]["title"]["content"]
    assert "用量可能不完整" in json.dumps(bridge._card(state, 1, done="failed"), ensure_ascii=False)


def test_usage_and_errors(bridge):
    state = bridge.RunState("dsh")
    for _ in range(2):
        state.observe(
            {
                "type": "status",
                "phase": "step_end",
                "usage": {
                    "inputTokens": 10,
                    "outputTokens": 2,
                    "cacheWriteTokens": 3,
                    "cacheReadTokens": 4,
                    "reasoningTokens": 1,
                },
            }
        )
    assert state.context_used() == 17
    assert state.total_usage["input_tokens"] == 20 and state.thinking == 2
    state.observe({"type": "error", "message": "broken"})
    assert state.stream_error == "broken"
    assert state.result is None
    state.observe(
        {"type": "status", "phase": "turn_end", "reason": {"kind": "interrupted"}}
    )
    state.returncode = 1
    assert not state.interrupted  # reason is only a label
    state.returncode = -15
    assert state.interrupted


@pytest.mark.parametrize(
    "value,expected",
    [
        ("ls -1", "ls -1"),
        ({"file_path": "a.txt"}, "a.txt"),
        ({"query": "query"}, "query"),
    ],
)
def test_tool_input_variants(value, expected):
    assert (
        harness.tool_summary({"tool": "future_tool", "input": value})
        == "future_tool: " + expected
    )


def test_migration_and_backup(bridge):
    old = {
        "session_id": "important",
        "created_at": "created",
        "last_used": "last",
        "cost_usd": 1.2,
        "turns_total": 2198,
        "seconds_total": 14085.6,
        "ctx_used": 610776,
        "workdir": "/tmp",
        "future": "keep",
    }
    path = Path(bridge.SESSION_FILE)
    raw = json.dumps({"version": 1, "chats": {"chat": old}})
    path.write_text(raw)
    bridge._chat_sessions = bridge._load_sessions()
    entry = bridge._chat_entry("chat")
    assert entry["sessions"]["claude"]["session_id"] == "important"
    assert entry["workdir"] == "/tmp" and entry["future"] == "keep"
    assert "session_id" not in entry
    normalized = json.dumps(entry)
    assert json.dumps(bridge._normalize_chat(entry)) == normalized
    backup = path.with_name("sessions.json.v1.bak")
    assert backup.read_text() == raw and backup.stat().st_mode & 0o777 == 0o600
    bridge._persist_sessions()
    assert json.loads(path.read_text())["version"] == 2
    bridge._load_sessions()
    assert backup.read_text() == raw


def test_switch_persist_and_new_scopes(bridge):
    bridge._chat_sessions["chat"] = {"session_id": "claude-id", "workdir": "/tmp"}
    bridge._capture_session("chat", "dsh", "dsh-id", "/tmp")
    bridge._handle_command(None, "/dsh", "chat")
    assert bridge._chat_harness("chat") == "dsh"
    assert bridge._load_sessions()["chat"]["harness"] == "dsh"
    bridge._handle_command(None, "/new", "chat")
    assert not bridge._session_entry("chat", "dsh").get("session_id")
    assert bridge._session_entry("chat", "claude")["session_id"] == "claude-id"
    bridge._handle_command(None, "/new all", "chat")
    assert not bridge._session_entry("chat", "claude").get("session_id")
    assert bridge._chat_workdir("chat") == "/tmp"


def test_unavailable_dsh_and_usage(bridge, monkeypatch):
    monkeypatch.setitem(harness.REGISTRY, "dsh", harness.Harness("dsh", "", "capture"))
    bridge._handle_command(None, "/dsh", "chat")
    assert bridge._chat_harness("chat") == "claude"
    assert "没有找到 dsh" in bridge._send_reply.call_args.args[1]
    bridge._handle_command(None, "/dsh hi", "chat")
    assert "用法" in bridge._send_reply.call_args.args[1]
    bridge._chat_entry("chat", create=True)["harness"] = "dsh"
    bridge._execute_and_reply(None, "hi", "chat")
    bridge._post_card_reply.assert_not_called()


def test_cd_preserves_session_and_warns(bridge, tmp_path):
    bridge._capture_session("chat", "dsh", "sid", "/tmp")
    bridge._cmd_cd(None, "chat", str(tmp_path))
    assert bridge._session_entry("chat", "dsh")["session_id"] == "sid"
    bridge._cmd_status(None, "chat")
    assert "目录与当前目录不同" in bridge._send_reply.call_args.args[1]


def test_epoch_prevents_rebind_but_keeps_stats(bridge):
    bridge._cmd_new(None, "chat", "dsh")
    bridge._capture_session("chat", "dsh", "stale", "/tmp", epoch=0)
    assert not bridge._session_entry("chat", "dsh").get("session_id")
    state = bridge.RunState("dsh")
    state.turns = 1
    bridge._update_chat_stats("chat", state, 10)
    assert bridge._session_entry("chat", "dsh")["turns_total"] == 1
    assert not bridge._session_entry("chat", "dsh").get("last_used")


def test_run_stdin_capture_resume_and_env(bridge, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_SECRET", "hidden")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "hidden")
    monkeypatch.setenv("DSH_PERMISSION_MODE", "danger-full-access")
    monkeypatch.setenv("CLAUDE_FEISHU_DSH_PERMISSION_MODE", "workspace-write")
    monkeypatch.setenv("DSH_HOME", "/tmp/dsh-home")
    proc = Proc(events())
    with mock.patch.object(bridge.subprocess, "Popen", return_value=proc) as popen:
        answer, state = bridge._run_dsh("- literal --json", "chat")
    assert "alpha.txt" in answer and state.returncode == 0
    args, kwargs = popen.call_args.args[0], popen.call_args.kwargs
    assert args == [sys.executable, "--profile", "headless", "--json", "-"]
    assert kwargs["stdin"] == subprocess.PIPE
    assert kwargs["start_new_session"]
    assert kwargs["env"]["DSH_PERMISSION_MODE"] == "workspace-write"
    assert kwargs["env"]["DSH_HOME"] == "/tmp/dsh-home"
    assert (
        "FEISHU_APP_SECRET" not in kwargs["env"]
        and "DEEPSEEK_API_KEY" not in kwargs["env"]
    )
    assert bridge._session_entry("chat", "dsh")["session_id"] == "session-fixture"
    with mock.patch.object(
        bridge.subprocess, "Popen", return_value=Proc(events())
    ) as popen:
        bridge._run_dsh("continue", "chat")
    assert popen.call_args.args[0][-3:] == ["--session-id", "session-fixture", "-"]


@pytest.mark.parametrize(
    "error",
    [
        'session "sid" was recorded in "/old", not "/new"',
        'session "sid" does not exist; omit --session-id to start a new Session',
        'session "sid" recorded no working directory, so it cannot be adopted',
    ],
)
def test_safe_session_recovery_once(bridge, error):
    bridge._capture_session("chat", "dsh", "sid", bridge.WORKDIR)
    fail = Proc([{"type": "error", "message": error}], rc=1, err=error)
    with mock.patch.object(
        bridge.subprocess, "Popen", side_effect=[fail, Proc(events())]
    ) as popen:
        _, state = bridge._run_dsh("hi", "chat")
    assert state.returncode == 0 and popen.call_count == 2
    assert "--session-id" in popen.call_args_list[0].args[0]
    assert "--session-id" not in popen.call_args_list[1].args[0]


def test_task_error_never_retries(bridge):
    bridge._capture_session("chat", "dsh", "sid", bridge.WORKDIR)
    proc = Proc([{"type": "error", "message": "file not found"}], rc=1)
    with mock.patch.object(bridge.subprocess, "Popen", return_value=proc) as popen:
        answer, state = bridge._run_dsh("hi", "chat")
    assert state.returncode == 1 and "file not found" in answer
    assert popen.call_count == 1


def test_new_during_session_creation(bridge):
    proc = Proc(events(), callback=lambda: bridge._cmd_new(None, "chat", "dsh"))
    with mock.patch.object(bridge.subprocess, "Popen", return_value=proc):
        bridge._run_dsh("hi", "chat")
    assert not bridge._session_entry("chat", "dsh").get("session_id")
    assert not bridge._session_entry("chat", "dsh").get("last_used")


def test_dsh_execution_route_and_initial_card(bridge):
    bridge._cmd_harness(None, "chat", "dsh")
    state = bridge.RunState("dsh")
    state.returncode = 0
    with mock.patch.object(bridge, "_run_dsh", return_value=("done", state)) as run:
        bridge._execute_and_reply(None, "hi", "chat")
    assert run.call_args.args == ("hi", "chat")
    assert (
        "DSH" in bridge._post_card_reply.call_args.args[1]["header"]["title"]["content"]
    )
    bridge._cmd_cost(None, "chat")
    assert "dsh 不上报" in bridge._send_reply.call_args.args[1]


def test_dsh_timeout_cleans_process(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "DSH_TIMEOUT", 0.15)
    monkeypatch.setattr(
        harness.Harness,
        "build_argv",
        lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    original = subprocess.Popen
    processes = []

    def spawn(*a, **kw):
        proc = original(*a, **kw)
        processes.append(proc)
        return proc

    with mock.patch.object(bridge.subprocess, "Popen", side_effect=spawn):
        with pytest.raises(subprocess.TimeoutExpired):
            bridge._run_dsh("hi", "chat")
    assert processes[0].poll() is not None
    assert bridge._active_process is None


def test_resumed_turn_numbers_are_not_double_counted(bridge):
    state = bridge.RunState("dsh")
    state.observe({"type": "status", "phase": "turn_start", "turn": 2198})
    state.observe({"type": "status", "phase": "step_start", "turn": 2198, "step": 2})
    state.observe({"type": "status", "phase": "turn_start", "turn": 2199})
    assert state.turns == 2
    bridge._update_chat_stats("chat", state, 3)
    assert bridge._session_entry("chat", "dsh")["turns_total"] == 2


def test_default_permission_is_readonly_and_invalid_mode_rejected(bridge, monkeypatch):
    monkeypatch.delenv("CLAUDE_FEISHU_DSH_PERMISSION_MODE", raising=False)
    monkeypatch.setenv("DSH_PERMISSION_MODE", "danger-full-access")
    assert bridge._build_env("dsh")["DSH_PERMISSION_MODE"] == "read-only"
    assert "DSH_PERMISSION_MODE" not in bridge._build_env("claude")
    monkeypatch.setenv("CLAUDE_FEISHU_DSH_PERMISSION_MODE", "danger-full-access")
    with pytest.raises(ValueError):
        bridge._build_env("dsh")


def test_session_is_bound_before_timeout(bridge, monkeypatch):
    monkeypatch.setattr(bridge, "DSH_TIMEOUT", 0.5)
    code = 'import json,time; print(json.dumps({"type":"session","sessionId":"early","cwd":"/tmp"}), flush=True); time.sleep(60)'
    monkeypatch.setattr(
        harness.Harness, "build_argv", lambda *a, **k: [sys.executable, "-c", code]
    )
    with pytest.raises(subprocess.TimeoutExpired):
        bridge._run_dsh("hi", "chat")
    assert bridge._session_entry("chat", "dsh")["session_id"] == "early"
    assert bridge._session_entry("chat", "dsh")["cwd"] == "/tmp"


@pytest.mark.parametrize("name", ["claude", "dsh"])
@pytest.mark.parametrize("done,starting", [(None, True), (None, False), ("done", False)])
def test_two_row_model_layout(bridge, monkeypatch, name, done, starting):
    """主标题 = harness + 状态 + 耗时；副标题 = 模型 · 目录 · 上下文。

    飞书 header 只有 title + subtitle 两行，所以模型/目录/上下文必须合成一行副标题，
    不再单独往正文插一行目录。降级路径仍要把两样都保留在正文里。
    """
    monkeypatch.setenv(f"CLAUDE_FEISHU_{name.upper()}_MODEL_NAME", "DeepSeek V4.1 Flash")
    state = bridge.RunState(name)
    state.workdir = str(bridge.HOME_DIR / "code")
    state.usage = {"input_tokens": 42000}
    card = bridge._card(state, 45, done=done, starting=starting)
    subtitle = card["header"]["subtitle"]["content"]
    assert subtitle.startswith("DeepSeek V4.1 Flash · ~/code · ctx ")
    if name == "dsh":
        assert subtitle == "DeepSeek V4.1 Flash · ~/code · ctx 42k"
    # 目录行已并入副标题，正文首行不再是它
    assert not card["elements"][0]["text"]["content"].startswith("~/code · ctx ")
    location = state.location_text()
    # 服务端拒副标题时：_without_subtitle 把整行副标题挪到正文首行
    fallback = bridge._without_subtitle(card)
    assert fallback["elements"][0]["text"]["content"] == subtitle
    # 之后重建的卡片（_subtitle_supported=False）拆成模型、目录两行，信息不丢
    monkeypatch.setattr(bridge, "_subtitle_supported", False)
    rebuilt = bridge._card(state, 45, done=done, starting=starting)
    assert rebuilt["elements"][0]["text"]["content"] == "DeepSeek V4.1 Flash"
    assert rebuilt["elements"][1]["text"]["content"] == location


def test_dsh_context_window_drives_percentage(bridge):
    """DSH 事件流不报窗口，只能由 CLAUDE_FEISHU_DSH_CONTEXT_WINDOW 声明。

    未配置（0）时只报绝对值，避免拿错窗口算出误导性占比；配置后与 claude 一样报百分比。
    """
    state = bridge.RunState("dsh")
    state.usage = {"input_tokens": 42000}
    assert state.context_window == bridge.DSH_CONTEXT_WINDOW
    state.context_window = 0
    assert state.location_text().endswith("ctx 42k")
    state.context_window = 1000000
    assert state.location_text().endswith("ctx 4%")
    # claude 侧不受影响，仍取 CLI 实际生效的窗口
    claude = bridge.RunState("claude")
    assert claude.context_window == bridge.CONTEXT_WINDOW
