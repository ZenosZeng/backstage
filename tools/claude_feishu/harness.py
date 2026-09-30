"""Headless harness commands and event adapters; no messaging or credentials."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import shutil
import signal


@dataclass(frozen=True)
class Harness:
    name: str
    binary: str
    session_mode: str

    def available(self) -> bool:
        return bool(self.binary and shutil.which(self.binary))

    def build_argv(self, task, *, session_id, is_new, workdir, settings=""):
        if self.name == "claude":
            cmd = [
                self.binary,
                "-p",
                task,
                "--max-turns",
                "200",
                "--settings",
                settings,
                "--output-format",
                "stream-json",
                "--verbose",
                "--include-partial-messages",
            ]
            if session_id:
                cmd += ["--session-id", session_id] if is_new else ["-r", session_id]
            return cmd
        cmd = [self.binary, "--profile", "headless", "--json"]
        if session_id:
            cmd += ["--session-id", session_id]
        # stdin avoids positional tasks beginning with CLI options.
        return cmd + ["-"]

    def session_from_event(self, event):
        if self.name == "dsh" and event.get("type") == "session":
            value = event.get("sessionId")
            if isinstance(value, str) and value:
                return value
        return None

    def env_allow(self):
        # Keys are read from DSH's private credentials file, never bridge env.
        return {"DSH_HOME", "TMPDIR"} if self.name == "dsh" else set()

    def recoverable(self, stderr):
        if self.name == "claude":
            return "session-missing" if "No conversation found" in stderr else ""
        for pattern, reason in (
            ("was recorded in", "cwd-mismatch"),
            ("does not exist; omit --session-id", "session-missing"),
            ("cannot be adopted", "not-adoptable"),
            (
                "is a subagent or forked session and cannot be driven directly",
                "not-adoptable",
            ),
            ("which the one-shot runner does not compose", "not-adoptable"),
        ):
            if pattern in stderr:
                return reason
        return ""

    def is_interrupt(self, returncode):
        return returncode in (
            -signal.SIGTERM,
            -signal.SIGKILL,
            128 + signal.SIGTERM,
            128 + signal.SIGKILL,
        )

    def observe(self, state, event):
        if self.name == "claude":
            state._observe_claude(event)
            return
        kind = event.get("type")
        truncated = bool(event.get("truncated"))
        if truncated:
            state.events_truncated = True
        if kind == "session":
            state.session_id = str(event.get("sessionId") or "")
            state.cwd = str(event.get("cwd") or "")
        elif kind == "status":
            phase = event.get("phase")
            if phase in {"turn_start", "step_start"}:
                turn = int(event.get("turn") or 1)
                # DSH numbers turns across resumed processes, not this run.
                if state.initial_turn is None:
                    state.initial_turn = turn
                state.turns = max(state.turns, turn - state.initial_turn + 1)
                if phase == "step_start":
                    state.step = int(event.get("step") or 0)
            elif phase == "step_end":
                usage = event.get("usage")
                if isinstance(usage, dict):
                    state.usage = {}
                    for source, dest in (
                        ("inputTokens", "input_tokens"),
                        ("outputTokens", "output_tokens"),
                        ("cacheReadTokens", "cache_read_input_tokens"),
                        ("cacheWriteTokens", "cache_creation_input_tokens"),
                        ("reasoningTokens", "reasoning_tokens"),
                    ):
                        value = usage.get(source)
                        if isinstance(value, (int, float)) and not isinstance(
                            value, bool
                        ):
                            state.usage[dest] = int(value)
                            if dest == "reasoning_tokens":
                                state.thinking += int(value)
                            state.total_usage[dest] = state.total_usage.get(
                                dest, 0
                            ) + int(value)
            elif phase == "turn_end":
                reason = event.get("reason")
                if isinstance(reason, dict):
                    state.terminal_reason = str(reason.get("kind") or "")
        elif kind == "thinking":
            state.thinking_text = str(event.get("text") or "")
        elif kind == "text":
            text = str(event.get("text") or "")
            if text:
                state.text_parts.append(text)
                state.last_result = text
                state.last_result_error = False
                state.last_output = "result"
                state.action = (
                    text.strip().splitlines()[0][:120] if text.strip() else ""
                )
        elif kind == "tool_call":
            state.add_step(tool_summary(event))
        elif kind == "tool_result":
            state.last_result = str(
                event.get("result") or ("（事件已截断）" if truncated else "")
            )
            state.last_result_error = event.get("status") == "error"
            state.last_output = "result"
        elif kind == "final":
            state.result = str(event.get("text") or "")
        elif kind == "error":
            state.stream_error = str(event.get("message") or "DSH error")
            state.last_result = state.stream_error
            state.last_result_error = True
            state.last_output = "result"


def tool_summary(event):
    name = str(event.get("tool") or "?")
    value = event.get("input")
    if isinstance(value, dict):
        detail = next(
            (
                value[k]
                for k in (
                    "command",
                    "file_path",
                    "path",
                    "pattern",
                    "query",
                    "url",
                    "description",
                    "prompt",
                    "skill",
                )
                if isinstance(value.get(k), str) and value[k].strip()
            ),
            "",
        )
        detail = detail or json.dumps(value, ensure_ascii=False)
    elif isinstance(value, str):
        detail = value
    else:
        detail = "（参数已截断）" if event.get("truncated") else ""
    detail = " ".join(detail.split())
    if len(detail) > 120:
        detail = detail[:120] + "…"
    return f"{name}: {detail}" if detail else name


REGISTRY = {
    "claude": Harness(
        "claude",
        os.environ.get("CLAUDE_CLI") or shutil.which("claude") or "",
        "preregister",
    ),
    "dsh": Harness(
        "dsh", os.environ.get("DSH_CLI") or shutil.which("dsh") or "", "capture"
    ),
}
