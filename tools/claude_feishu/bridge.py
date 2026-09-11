#!/usr/bin/env python3
"""Feishu bot bridge: remote-control Claude Code from a Feishu chat (P4).

双向遥控：在飞书里私聊机器人（或群聊 @它）发指令 → lark-oapi 长连接
（WS，无需公网回调）接收 → 白名单校验 → 本机 `claude -p` 执行 →
结果回复到原消息线程。

安全设计（codex 审计 2026-08-07 后修复）：
- 凭证只从环境变量读（start 从 0600 的 ~/.config/claude-feishu/env 加载）
- 白名单为空时拒绝启动（除非显式 FEISHU_OPEN_MODE=1）
- claude 子进程使用最小 env 白名单 + 独立 settings（--settings）
- 超时按进程组 TERM/KILL（start_new_session）
- 日志不记录 prompt 原文（只记长度+哈希）；SDK 日志降级 WARN

运行：在 .agents 下使用 pixi run claude-feishu；配置见 tools/README.md。
"""

from __future__ import annotations

import os
import sys
import json
import time
import uuid
import signal
import hashlib
import logging
import pathlib
import queue
import shutil
import subprocess
import threading
import urllib.request
from typing import Callable

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
HOME_DIR = pathlib.Path.home()
from collections import deque

from lark_oapi import EventDispatcherHandler
from lark_oapi.core.enum import LogLevel
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
import lark_oapi.ws as ws

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
BOT_OPEN_ID = os.environ.get("FEISHU_BOT_OPEN_ID", "")  # 群聊提及校验
CLAUDE = os.environ.get(
    "CLAUDE_CLI",
    shutil.which("claude") or str(HOME_DIR / ".pixi/envs/nodejs/bin/claude"),
)
SETTINGS = os.environ.get(
    "CLAUDE_FEISHU_SETTINGS", str(SCRIPT_DIR / "claude-settings.json")
)
WORKDIR = os.environ.get("CLAUDE_FEISHU_WORKDIR", str(HOME_DIR / "code"))
ALLOW_OPEN_IDS = {
    s for s in os.environ.get("FEISHU_ALLOW_OPEN_IDS", "").split(",") if s
}
# 审计 C3：默认 fail-closed；显式 FEISHU_OPEN_MODE=1 才允许空白名单（不推荐）
OPEN_MODE = os.environ.get("FEISHU_OPEN_MODE", "") == "1"
MAX_REPLY = 3800  # 单条消息字符上限（长回复分块发送）
EXEC_TIMEOUT = int(os.environ.get("CLAUDE_FEISHU_TIMEOUT", "600"))  # 单条指令上限
PROGRESS_INTERVAL = int(
    os.environ.get("CLAUDE_FEISHU_PROGRESS_INTERVAL", "600")
)  # 执行中的进行中提醒间隔（保底；流式进度见下）
# 流式进度：解析 claude 的 stream-json 事件，按此间隔刷新同一张飞书卡片。
# 设 0 关闭流式进度（退回只有结束才回复的旧行为）。
STREAM_INTERVAL = int(os.environ.get("CLAUDE_FEISHU_STREAM_INTERVAL", "5"))
THINKING_MAX = 200  # 进度卡片里思考摘要的字数上限（手机端一屏内）
STEPS_MAX = 40  # RunState 保留的步骤条数（内存上限）
STEPS_SHOWN = 5  # 进度卡片里展示的最近步骤条数
OUTPUT_MAX = 320  # 进度卡片里"最新输出"（工具结果/正文）的字数上限
CARD_BODY_MAX = 3500  # 收尾卡片正文上限，超出部分随后分条补发
QUEUE_MAX = 5  # 每个 chat 的排队指令上限
def _read_settings_env() -> dict:
    """~/.claude/settings.json 的 env 块——CLI 实际生效的配置来源。

    注意不是 --settings 指向的那个文件（claude-settings.json 只有 129B、无 env
    块）；模型与 effort 都配在 ~/.claude/settings.json 里。
    """
    try:
        data = json.loads((HOME_DIR / ".claude/settings.json").read_text())
    except Exception:  # noqa: BLE001 - 读不到就当没配，不影响执行
        return {}
    env = data.get("env")
    return env if isinstance(env, dict) else {}


_SETTINGS_ENV = _read_settings_env()


def _setting(name: str, default: str = "") -> str:
    """取生效配置：进程环境优先，其次 ~/.claude/settings.json 的 env 块。

    effort 等变量只配在 settings.json 里、不在 bridge 进程环境里，只读
    os.environ 会取空。
    """
    return os.environ.get(name) or str(_SETTINGS_ENV.get(name) or default)


# 上下文窗口大小：优先取 CLI 实际生效的 CLAUDE_CODE_MAX_CONTEXT_TOKENS，
# 没有就只报已用量、不报百分比（避免拿错窗口算出误导性的占比）。
CONTEXT_WINDOW = int(_setting("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "0") or 0)
EFFORT = _setting("CLAUDE_CODE_EFFORT_LEVEL")


def _short_path(path: str) -> str:
    """家目录缩成 ~，标题栏里省地方。"""
    home = str(HOME_DIR)
    return "~" + path[len(home):] if path.startswith(home) else path


def _clip(text: str, limit: int) -> str:
    """压掉换行/多空格并截断——卡片一行放不下长文本。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


def _fmt_duration(elapsed: float) -> str:
    """执行时长的紧凑写法：min+s 两个单位（2min13s / 45s）。"""
    mins, secs = divmod(int(elapsed), 60)
    return f"{mins}min{secs:02d}s" if mins else f"{secs}s"


def _clip_tail(text: str, limit: int) -> str:
    """保留末尾——流式正文/长输出里，最新的部分才是有信息量的。"""
    text = text.strip()
    return text if len(text) <= limit else "…" + text[-limit:]


# 工具调用摘要里每种工具取哪个入参字段（其余字段对"在干什么"没信息量）
_TOOL_DETAIL_KEYS = {
    "Bash": ("command",),
    "Read": ("file_path",),
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "NotebookEdit": ("notebook_path", "file_path"),
    "Grep": ("pattern",),
    "Glob": ("pattern",),
    "Task": ("description",),
    "Agent": ("description",),
    "WebFetch": ("url",),
    "WebSearch": ("query",),
    "Skill": ("skill",),
}
_TOOL_DETAIL_MAX = 120


def _tool_summary(part: dict) -> str:
    """工具调用的一行摘要：Bash 带命令、Read/Edit 带路径、Grep 带模式。

    只写工具名的话卡片上永远只有"调用 Bash"，看不出在干什么。
    """
    name = str(part.get("name") or "?")
    data = part.get("input") if isinstance(part.get("input"), dict) else {}
    detail = ""
    for key in _TOOL_DETAIL_KEYS.get(name, ()):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            detail = value
            break
    if not detail and data:
        detail = json.dumps(data, ensure_ascii=False)
    return f"{name}: {_clip(detail, _TOOL_DETAIL_MAX)}" if detail else name
# 打断词：执行中再发这些内容即终止当前进程组（其余消息仍回"忙"提示）
INTERRUPT_WORDS = {
    w.strip().lower()
    for w in os.environ.get("CLAUDE_FEISHU_INTERRUPT_WORDS", "停,停止,打断,中断,取消,stop,cancel").split(",")
    if w.strip()
}
# 去重持久化（审计 H7：重启后不丢）
SEEN_FILE = os.environ.get(
    "CLAUDE_FEISHU_STATE", str(HOME_DIR / ".local/state/claude-feishu-seen.json")
)
# 会话上下文：按 chat 持久化 claude 原生会话（-r/--session-id），跨消息有上下文。
# 文件只存 chat_id -> session_id 映射（几十字节），对话本体由 claude 自己管理；
# 设 FEISHU_SESSION_CONTEXT=0 可关闭，退回每次全新会话的旧行为。
SESSION_FILE = os.environ.get(
    "CLAUDE_FEISHU_SESSIONS", str(HOME_DIR / ".local/state/claude-feishu-sessions.json")
)
SESSION_CONTEXT_ENABLED = os.environ.get("FEISHU_SESSION_CONTEXT", "1") != "0"


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a positive integer, got {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} must be > 0, got {value}")
    return value


SEEN_LIMIT = _positive_int_env("FEISHU_SEEN_LIMIT", 2000)
MAX_EVENT_AGE_SECONDS = _positive_int_env("FEISHU_MAX_EVENT_AGE_SECONDS", 30)
_STARTED_AT_MS = int(time.time() * 1000)

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s")
_lock = threading.Lock()  # 同时只执行一条遥控指令
_queue_lock = threading.Lock()
_chat_queues: dict[str, deque] = {}  # chat_id -> 排队的 (event, 指令)
_drain_running = False  # 单消费者：同一时刻最多一个 drain 线程（_queue_lock 保护）
_token_lock = threading.Lock()
_process_lock = threading.Lock()
_stopping = threading.Event()
_interrupt_requested = threading.Event()  # 本轮的打断请求（跨 _run_claude 传递）
_active_process = None
_token: dict[str, object] = {"token": "", "expires": 0.0}


# ---------------------------------------------------------------------------
# 防重放：持久化 message_id、event_id 和稳定事件指纹
# ---------------------------------------------------------------------------


def _load_seen() -> tuple[list[str], list[str], list[str]]:
    state_path = pathlib.Path(SEEN_FILE)
    if not state_path.exists():
        return [], [], []
    try:
        data = json.loads(state_path.read_text())
        if isinstance(data, list):  # v1: message_id list
            if all(isinstance(item, str) for item in data):
                return list(data), [], []
            raise ValueError("legacy state contains non-string message IDs")
        if not isinstance(data, dict) or data.get("version") != 2:
            raise ValueError("unsupported state schema")
        identity_lists = tuple(
            data.get(key, []) for key in ("message_ids", "event_ids", "fingerprints")
        )
        if not all(
            isinstance(items, list) and all(isinstance(item, str) for item in items)
            for items in identity_lists
        ):
            raise ValueError("state identity lists must contain only strings")
        return identity_lists
    except Exception as exc:  # noqa: BLE001
        # An unreadable dedup ledger must never silently become an empty ledger.
        raise RuntimeError(f"invalid seen state {state_path}: {exc}") from exc


_loaded_message_ids, _loaded_event_ids, _loaded_fingerprints = _load_seen()
_seen_msg_ids: deque[str] = deque(_loaded_message_ids, maxlen=SEEN_LIMIT)
_seen_event_ids: deque[str] = deque(_loaded_event_ids, maxlen=SEEN_LIMIT)
_seen_fingerprints: deque[str] = deque(_loaded_fingerprints, maxlen=SEEN_LIMIT)
_seen_lock = threading.Lock()


def _event_fingerprint(data: P2ImMessageReceiveV1) -> str:
    msg = data.event.message
    sender_id = (
        getattr(getattr(data.event.sender, "sender_id", None), "open_id", "") or ""
    )
    payload = json.dumps(
        {
            "sender": sender_id,
            "chat": msg.chat_id or "",
            "created": str(
                msg.create_time or getattr(data.header, "create_time", "") or ""
            ),
            "type": msg.message_type or "",
            "content": msg.content or "",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _event_created_at_ms(data: P2ImMessageReceiveV1) -> int | None:
    values = (
        getattr(data.event.message, "create_time", None),
        getattr(getattr(data, "header", None), "create_time", None),
    )
    for value in values:
        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            continue
        if timestamp < 10_000_000_000:
            timestamp *= 1000
        return timestamp
    return None


def _stale_event_reason(
    data: P2ImMessageReceiveV1, *, now_ms: int | None = None
) -> str | None:
    created_at_ms = _event_created_at_ms(data)
    if created_at_ms is None:
        return "missing create_time"
    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if created_at_ms < _STARTED_AT_MS - 5000:
        return "predates bridge startup"
    age_ms = current_ms - created_at_ms
    if age_ms > MAX_EVENT_AGE_SECONDS * 1000:
        return f"age={age_ms / 1000:.1f}s"
    if age_ms < -30_000:
        return f"future timestamp={-age_ms / 1000:.1f}s"
    return None


def _persist_seen() -> None:
    state_path = pathlib.Path(SEEN_FILE)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
    payload = {
        "version": 2,
        "message_ids": list(_seen_msg_ids),
        "event_ids": list(_seen_event_ids),
        "fingerprints": list(_seen_fingerprints),
    }
    temp_path.write_text(json.dumps(payload, separators=(",", ":")))
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, state_path)


def _mark_seen(msg_id: str, event_id: str, fingerprint: str) -> bool:
    """Atomically persist all stable event identities before execution."""
    with _seen_lock:
        if (
            (msg_id and msg_id in _seen_msg_ids)
            or (event_id and event_id in _seen_event_ids)
            or (fingerprint and fingerprint in _seen_fingerprints)
        ):
            return False
        if msg_id:
            _seen_msg_ids.append(msg_id)
        if event_id:
            _seen_event_ids.append(event_id)
        if fingerprint:
            _seen_fingerprints.append(fingerprint)
        _persist_seen()
        return True


# ---------------------------------------------------------------------------
# token（审计 M9：加锁 + 异常不外逃）
# ---------------------------------------------------------------------------


def _get_token() -> str:
    with _token_lock:
        if _token["expires"] > time.time() + 60:
            return str(_token["token"])
        payload = json.dumps({"app_id": APP_ID, "app_secret": APP_SECRET}).encode()
        req = urllib.request.Request(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
        if body.get("code") != 0:
            raise RuntimeError(
                f"token error: code={body.get('code')} msg={body.get('msg')}"
            )
        _token["token"] = body["tenant_access_token"]
        _token["expires"] = time.time() + body["expire"] - 60
        return str(_token["token"])


# ---------------------------------------------------------------------------
# 回复（审计 M8：用 reply API 真正挂线程；L13：长回复分块）
# ---------------------------------------------------------------------------


def _post_reply(data: P2ImMessageReceiveV1, text: str) -> bool:
    msg = data.event.message
    payload = {
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{msg.message_id}/reply"
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_get_token()}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
        if body.get("code") != 0:
            logging.error("reply failed: %s", body)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        logging.error("reply exception: %s", exc)
        return False


def _extract_message_id(body: dict) -> str | None:
    data = body.get("data") if isinstance(body, dict) else None
    return (data or {}).get("message_id") or None


# header.subtitle 是卡片 1.0 的可选字段；万一服务端不认，就降级成不带副标题的
# 卡片，而不是整张卡片发不出去、直接退化成纯文本。
_subtitle_supported = True


def _without_subtitle(card: dict) -> dict:
    header = {k: v for k, v in (card.get("header") or {}).items() if k != "subtitle"}
    return {**card, "header": header}


def _card_api(url: str, card: dict, method: str) -> tuple[bool, dict]:
    """调用飞书卡片接口（POST reply / PATCH 更新），返回 (是否成功, 响应体)。"""
    data = {"content": json.dumps(card, ensure_ascii=False)}
    if method == "POST":
        data = {"msg_type": "interactive", **data}
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {_get_token()}",
            },
            method=method,
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        logging.error("card %s exception: %s", method, exc)
        return False, {}
    if body.get("code") != 0:
        logging.error("card %s failed: %s", method, body)
        return False, body
    return True, body


def _call_card_api(url: str, card: dict, method: str) -> tuple[bool, dict]:
    """带副标题降级：被拒时去掉 subtitle 重试一次，并记住以后不再带。"""
    global _subtitle_supported
    ok, body = _card_api(url, card, method)
    if ok or not _subtitle_supported or "subtitle" not in (card.get("header") or {}):
        return ok, body
    logging.warning("card %s rejected with header.subtitle; retrying without", method)
    ok, body = _card_api(url, _without_subtitle(card), method)
    if ok:
        _subtitle_supported = False
        logging.warning("header.subtitle unsupported; statusline dropped from cards")
    return ok, body


def _post_card_reply(data: P2ImMessageReceiveV1, card: dict) -> str | None:
    """以交互卡片形式回复，返回新消息 id（供后续原地更新）；失败返回 None。"""
    msg = data.event.message
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{msg.message_id}/reply"
    ok, body = _call_card_api(url, card, "POST")
    return _extract_message_id(body) if ok else None


def _patch_card(message_id: str, card: dict) -> bool:
    """原地更新一张已发出的卡片（PATCH /im/v1/messages/{id}）。"""
    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}"
    ok, _ = _call_card_api(url, card, "PATCH")
    return ok


class RunState:
    """执行期的可观测状态：模型、思考、当前动作、步骤历史、轮次与用量。

    来源是 claude 的 stream-json 事件；全部字段都是"有则更新"，缺事件不影响
    最终结果——最终回答始终以 result 事件（或累积文本兜底）为准。
    """

    def __init__(self) -> None:
        self.model = ""
        self.workdir = ""  # 本次执行的工作目录（/cd 切换后与默认值不同）
        self.thinking = 0
        self.thinking_text = ""  # 最新一段思考正文（thinking 块）
        self.live_thinking = ""  # 正在流式生成的思考（partial messages）
        self.live_text = ""  # 正在流式生成的正文（partial messages）
        self.last_result = ""  # 最近一次工具输出（命令输出/报错）
        self.last_result_error = False
        self.last_output = ""  # "text" | "result"：最新输出区显示哪个
        self.action = ""
        self.steps: list[str] = []  # 滚动步骤历史（工具调用一行摘要）
        self.turns = 0
        self.usage: dict = {}  # 最后一次 assistant 调用的用量（= 当前上下文长度）
        self.total_usage: dict = {}  # result 事件的累计用量（整轮所有调用之和）
        self.cost: float | None = None
        self.result: str | None = None
        self.text_parts: list[str] = []
        self.returncode: int | None = None
        self.interrupt_requested = False

    def observe(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "system":
            if event.get("subtype") == "init":
                self.model = str(event.get("model") or self.model)
            elif event.get("subtype") == "thinking_tokens":
                self.thinking = max(self.thinking, int(event.get("estimated_tokens") or 0))
        elif kind == "stream_event":
            self._observe_stream(event.get("event") or {})
        elif kind == "user":
            self._observe_tool_result(event)
        elif kind == "assistant":
            self.turns += 1
            msg = event.get("message") or {}
            # 用量随每条 assistant 事件下发；result 事件要到整轮结束才有，
            # 只认 result 的话执行中的卡片永远显示不出上下文占比。
            if isinstance(msg.get("usage"), dict):
                self.usage = msg["usage"]
            for part in msg.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "thinking":
                    # 思考正文只留最新一段；空块（redacted）不覆盖上一段。
                    text = str(part.get("thinking") or "").strip()
                    if text:
                        self.thinking_text = text
                elif part.get("type") == "text" and part.get("text"):
                    self.text_parts.append(str(part["text"]))
                    self.action = str(part["text"]).strip().splitlines()[0][:120]
                elif part.get("type") == "tool_use":
                    self.action = _tool_summary(part)
                    self.steps.append(self.action)
                    del self.steps[:-STEPS_MAX]
        elif kind == "result":
            self.result = event.get("result") if isinstance(event.get("result"), str) else self.result
            # result 的 usage 是整轮所有 API 调用的累计值（实测 = 各次 assistant
            # 之和），不是上下文长度——用它算 ctx 会随调用次数虚高（同一会话里
            # 3 次调用就 3 倍）。所以只留作统计，占比始终以最后一次 assistant
            # 事件的 usage 为准。
            if isinstance(event.get("usage"), dict):
                self.total_usage = event["usage"]
            if isinstance(event.get("total_cost_usd"), (int, float)):
                self.cost = float(event["total_cost_usd"])
            if isinstance(event.get("num_turns"), int):
                self.turns = max(self.turns, int(event["num_turns"]))

    def _observe_stream(self, ev: dict) -> None:
        """partial message 事件（--include-partial-messages）：逐字累积正文/思考。

        有了这个才能在卡片上看到"正在写的字"，而不是每 5 秒才跳一次的快照。
        """
        etype = ev.get("type")
        if etype == "content_block_start":
            block = ev.get("content_block") or {}
            if block.get("type") == "text":
                self.live_text = ""
            elif block.get("type") == "thinking":
                self.live_thinking = ""
        elif etype == "content_block_delta":
            delta = ev.get("delta") or {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                self.live_text += str(delta.get("text") or "")
                self.last_output = "text"
            elif dtype == "thinking_delta":
                self.live_thinking += str(delta.get("thinking") or "")

    def _observe_tool_result(self, event: dict) -> None:
        """user 事件里的 tool_result：终端里最常看的东西（命令输出/报错）。"""
        msg = event.get("message") or {}
        for part in msg.get("content") or []:
            if not isinstance(part, dict) or part.get("type") != "tool_result":
                continue
            content = part.get("content")
            if isinstance(content, list):
                text = "\n".join(
                    str(block.get("text") or "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                text = str(content or "")
            text = text.strip()
            if not text:
                continue
            self.last_result = text
            self.last_result_error = bool(part.get("is_error"))
            self.last_output = "result"

    def latest_output(self) -> str:
        """终端里最常盯的那块：正在写的正文，或最近一次工具输出。"""
        if self.last_output == "text" and self.live_text.strip():
            return _clip_tail(self.live_text, OUTPUT_MAX)
        if self.last_output == "result" and self.last_result:
            return _clip(self.last_result, OUTPUT_MAX)
        if self.live_text.strip():
            return _clip_tail(self.live_text, OUTPUT_MAX)
        return _clip(self.last_result, OUTPUT_MAX) if self.last_result else ""

    def answer(self) -> str:
        if self.result is not None:
            return self.result
        if self.text_parts:
            return "\n".join(self.text_parts).strip()
        return self.live_text.strip()  # 只有 partial 事件时兜底

    @property
    def interrupted(self) -> bool:
        """本轮是否被 _request_interrupt 打断。

        claude CLI 抓到 SIGTERM 后是自己退出的，rc=143（128+15）而不是 -15，
        只认负信号值会漏判（实测日志里请求了打断却记成 interrupted=False），
        所以以显式标记为准、信号值兜底。
        """
        if self.interrupt_requested:
            return True
        return self.returncode in (
            -signal.SIGTERM, -signal.SIGKILL, 128 + signal.SIGTERM, 128 + signal.SIGKILL
        )

    def progress_md(self, elapsed: float) -> str:
        """进度卡片正文：统计 + 思考摘要 + 当前动作 + 最近步骤。

        耗时在卡片主标题、模型/effort/路径/上下文占比在副标题，正文都不重复。
        手机上要短：思考取最新一段的前 THINKING_MAX 字，历史步骤只列最近
        STEPS_SHOWN 条。
        """
        stats = []
        if self.turns:
            stats.append(f"轮次 {self.turns}")
        if self.thinking >= 1000:
            stats.append(f"思考 {self.thinking / 1000:.1f}k")
        elif self.thinking:
            stats.append(f"思考 {self.thinking}")
        lines = [" · ".join(stats)] if stats else []
        # 思考：优先流式缓冲（正在写的那段），其次 assistant 事件里的完整块
        thinking = self.live_thinking.strip() or self.thinking_text
        if thinking:
            lines.append(f"💭 {_clip(thinking, THINKING_MAX)}")
        if self.action:
            lines.append(f"- 当前：{self.action}")
        output = self.latest_output()
        if output:
            # 工具输出保留换行（命令回显/报错按行读更清楚），正文压平
            body = output if self.last_output == "result" else " ".join(output.split())
            mark = "⚠️ " if self.last_result_error and self.last_output == "result" else ""
            lines.append("**最新输出**")
            lines.append(f"{mark}{body}")
        # 当前动作若是工具调用，已在"当前"行显示，历史里不再重复最后一条
        history = self.steps[:-1] if self.steps and self.steps[-1] == self.action else self.steps
        history = history[-STEPS_SHOWN:]
        if history:
            lines.append("**最近步骤**")
            lines.extend(f"{i}. {s}" for i, s in enumerate(history, 1))
        return "\n".join(lines)

    def context_used(self) -> int:
        """本轮上下文占用 = 本轮送进模型的全部 token。

        含未命中缓存的新输入 + 命中缓存的复用部分 + 本轮新建缓存；三者之和
        才是这次请求实际占用的上下文长度。
        """
        return sum(
            int(self.usage.get(key) or 0)
            for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )

    def _ctx_text(self) -> str:
        used = self.context_used()
        if not used:
            return ""
        if not CONTEXT_WINDOW:
            return f"ctx {used / 1000:.0f}k"  # 不知窗口就只报绝对量，不算百分比
        return f"ctx {used / CONTEXT_WINDOW * 100:.0f}%"

    def statusline(self) -> str:
        """卡片标题栏（副标题）内容：模型+effort · 工作路径 · 上下文占比。

        飞书 header 的 subtitle 最多一行，手机端尤其要短：effort 与模型拼在
        一起（"deepseek-flash max"）、不写机器名；耗时/轮次在正文里。
        """
        model = self.model or _setting("ANTHROPIC_MODEL") or "?"
        bits = [f"{model} {EFFORT}".strip()]
        bits.append(_short_path(self.workdir or WORKDIR))
        ctx = self._ctx_text()
        if ctx:
            bits.append(ctx)
        return " · ".join(bits)


def _usage_note(state: RunState) -> str:
    """收尾卡脚注：轮次 / 输出 token / 成本（终端里 /cost 给的信息）。"""
    bits = []
    if state.turns:
        bits.append(f"{state.turns} 轮")
    out_tokens = int((state.total_usage or {}).get("output_tokens") or 0)
    if out_tokens >= 1000:
        bits.append(f"输出 {out_tokens / 1000:.1f}k tok")
    elif out_tokens:
        bits.append(f"输出 {out_tokens} tok")
    if state.cost is not None:
        bits.append(f"${state.cost:.4f}")
    return " · ".join(bits)


def _card(
    state: RunState, elapsed: float, *, done: str | None = None, starting: bool = False
) -> dict:
    """构建进度/结果卡片。done 非空表示收尾态。

    状态词放主标题、statusline 放副标题（飞书 header 的 subtitle 最多一行）：
    进度与收尾两种态都带同一行状态栏，位置固定在顶部、不随正文长度跑。
    标题按实际结局着色，打断/失败不能顶着绿色的"执行完成"。
    """
    if done is None:
        title, template = f"🤖 执行中 · {_fmt_duration(elapsed)}", "blue"
        body = "已接收，正在启动…" if starting else state.progress_md(elapsed)
        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]
    else:
        if state.interrupted:
            title, template = "🛑 已打断", "orange"
        elif state.returncode not in (0, None):
            title, template = f"⚠️ 执行失败 (rc={state.returncode})", "red"
        else:
            title, template = "✅ 执行完成", "green"
        title += f" · {_fmt_duration(elapsed)}"  # 耗时进主标题，正文里不再重复
        body = (
            done
            if len(done) <= CARD_BODY_MAX
            else done[:CARD_BODY_MAX] + "\n…（正文过长，剩余部分随后发出）"
        )
        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": body}}]
        note = _usage_note(state)
        if note:
            elements.append(
                {"tag": "note", "elements": [{"tag": "plain_text", "content": note}]}
            )
    header = {
        "template": template,
        "title": {"tag": "plain_text", "content": title},
    }
    if _subtitle_supported:
        header["subtitle"] = {"tag": "plain_text", "content": state.statusline()}
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": header,
        "elements": elements,
    }


def _send_reply(data: P2ImMessageReceiveV1, text: str) -> bool:
    """Reply into the same chat thread; long text split into chunks."""
    if not text:
        return True
    chunks = [text[i : i + MAX_REPLY] for i in range(0, len(text), MAX_REPLY)]
    ok = True
    for index, chunk in enumerate(chunks, 1):
        body = chunk if len(chunks) == 1 else f"({index}/{len(chunks)})\n{chunk}"
        ok = _post_reply(data, body) and ok
    return ok


# ---------------------------------------------------------------------------
# 会话上下文（2026-08-07）：chat_id -> claude session_id
# 与 seen 账本不同，本文件损坏只丢上下文、不丢安全属性，故 fail-open 仅告警。
# 所有读写都在 _execute_and_reply 的 _lock 内（单条执行串行化），无需额外锁。
# ---------------------------------------------------------------------------


def _load_sessions() -> dict[str, dict[str, str]]:
    state_path = pathlib.Path(SESSION_FILE)
    if not state_path.exists():
        return {}
    try:
        data = json.loads(state_path.read_text())
        chats = data.get("chats", {})
        if not isinstance(chats, dict):
            raise ValueError("chats must be a dict")
        return chats
    except Exception as exc:  # noqa: BLE001
        logging.warning(
            "session state unreadable (%s); context will restart fresh", exc
        )
        return {}


_chat_sessions: dict[str, dict[str, str]] = _load_sessions()
_sessions_lock = threading.RLock()  # 保护 _chat_sessions 的读改写与落盘


def _persist_sessions() -> None:
    """原子写会话状态。

    审计 P2：命令线程（/cd /new）与执行线程会同时保存，此前共用
    `.{name}.{pid}.tmp` 一个临时文件名，先 replace 的线程会让另一个抛
    FileNotFoundError。加锁串行化，并把线程号写进临时名避免互踩。
    """
    with _sessions_lock:
        state_path = pathlib.Path(SESSION_FILE)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = state_path.with_name(
            f".{state_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        payload = {"version": 1, "chats": _chat_sessions}
        temp_path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, state_path)


def _get_or_create_session(chat_id: str) -> tuple[str | None, bool]:
    """返回该 chat 的 claude session_id 及是否新建。

    新建时先登记再执行（执行中途崩溃也不丢 id）；返回 (None, False) 表示不启用。
    保留 entry 里的其它字段（/cd 设的 workdir、累计统计），不整体覆盖。
    """
    if not SESSION_CONTEXT_ENABLED or not chat_id:
        return None, False
    with _sessions_lock:
        entry = _chat_sessions.get(chat_id)
        if entry and entry.get("session_id"):
            return str(entry["session_id"]), False
        session_id = str(uuid.uuid4())
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        merged = dict(entry or {})
        merged.update(
            {"session_id": session_id, "created_at": merged.get("created_at", now), "last_used": now}
        )
        _chat_sessions[chat_id] = merged
        _persist_sessions()
        return session_id, True


def _chat_entry(chat_id: str, *, create: bool = False) -> dict:
    """该 chat 的持久化条目（会话 id / 工作目录 / 累计统计）。"""
    with _sessions_lock:
        entry = _chat_sessions.get(chat_id)
        if entry is None:
            if not create or not chat_id:
                return {}
            entry = {}
            _chat_sessions[chat_id] = entry
        return entry


def _chat_workdir(chat_id: str) -> str:
    """该 chat 的工作目录；/cd 未设过则用默认 WORKDIR。"""
    entry = _chat_sessions.get(chat_id) or {}
    workdir = str(entry.get("workdir") or "").strip()
    return workdir or WORKDIR


def _update_chat_stats(chat_id: str, state: RunState, elapsed: float) -> None:
    """把本轮的成本/上下文/轮次累加进会话条目，供 /cost /status 查询。"""
    if not chat_id:
        return
    with _sessions_lock:
        entry = _chat_sessions.get(chat_id)
        if entry is None:
            return
        if state.cost is not None:
            entry["cost_usd"] = round(float(entry.get("cost_usd") or 0.0) + state.cost, 6)
        entry["turns_total"] = int(entry.get("turns_total") or 0) + state.turns
        entry["seconds_total"] = round(float(entry.get("seconds_total") or 0.0) + elapsed, 1)
        used = state.context_used()
        if used:
            entry["ctx_used"] = used
        entry["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _persist_sessions()


# ---------------------------------------------------------------------------
# claude 子进程（审计 C2：最小 env 白名单；H6：进程组终止；H5：独立 settings）
# ---------------------------------------------------------------------------

# 只保留运行 claude 必需的变量；FEISHU_*/AWS_*/云存储/Coder 凭证一律不带
_ENV_ALLOW = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TERM",
    "KUBECONFIG",
    "CONDA_OVERRIDE_CUDA",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "CLAUDE_CODE_SSE_PORT",
}
_ENV_ALLOW_PREFIXES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)


def _build_env() -> dict[str, str]:
    env = {}
    for key, value in os.environ.items():
        if key in _ENV_ALLOW or any(key.startswith(p) for p in _ENV_ALLOW_PREFIXES):
            env[key] = value
    return env


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.communicate(timeout=10)
    except Exception:  # noqa: BLE001 - 进程可能已退出
        pass


def _run_claude(
    prompt: str, chat_id: str = "", on_progress: Callable[[RunState, float], None] | None = None
) -> tuple[str, RunState]:
    """Execute the prompt headless in a fresh process group; returns (回复, 状态)。

    会话上下文：同一 chat 复用 claude 原生会话（新建用 --session-id 固定 id，
    之后 -r 恢复），对话历史（含工具调用）由 claude 自己持久化并自动压缩。
    会话被外部清理（~/.claude/projects 被删）时自动重建一次。

    进度：用 --output-format stream-json 逐行读事件（而非 communicate 缓冲到
    结束），据此每 STREAM_INTERVAL 秒回调一次 on_progress(state, 已等待秒数)；
    最终回答取 result 事件，缺失时回退到累积文本。总时长不超过 EXEC_TIMEOUT。
    打断：进程组被外部终止时 stdout 直接 EOF，按中断返回。
    """
    global _active_process
    _interrupt_requested.clear()  # 只算本轮的打断请求
    workdir = _chat_workdir(chat_id)
    while not _stopping.is_set():
        session_id, is_new = _get_or_create_session(chat_id)
        cmd = [
            CLAUDE, "-p", prompt, "--max-turns", "200", "--settings", SETTINGS,
            "--output-format", "stream-json", "--verbose",
            # 逐字流式：卡片才能显示"正在写的字"，而不是 5s 一跳的快照
            "--include-partial-messages",
        ]
        if session_id:
            cmd += ["--session-id", session_id] if is_new else ["-r", session_id]
        with _process_lock:
            if _stopping.is_set():
                return "Service is stopping", RunState()
            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                env=_build_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            _active_process = proc
        state = RunState()
        state.workdir = workdir
        raw_lines: list[str] = []
        try:
            started = time.monotonic()
            deadline = started + EXEC_TIMEOUT

            # stderr 单独线程抽干，避免子进程写满 stderr 管道后阻塞
            err_buf: list[str] = []

            def _drain_stderr() -> None:
                try:
                    for line in proc.stderr:  # type: ignore[union-attr]
                        err_buf.append(line)
                except Exception:  # noqa: BLE001 - 管道关闭即结束
                    pass

            threading.Thread(target=_drain_stderr, daemon=True).start()

            # 读线程 + 队列：超时与进度回调不能依赖"有输出"——claude 挂住不产出时
            # 直接 for line in proc.stdout 会永久阻塞，EXEC_TIMEOUT 永远不触发。
            lines_q: queue.Queue = queue.Queue()

            def _pump_stdout() -> None:
                try:
                    for raw in proc.stdout:  # type: ignore[union-attr]
                        lines_q.put(raw)
                except Exception:  # noqa: BLE001 - 管道关闭即结束
                    pass
                finally:
                    lines_q.put(None)

            threading.Thread(target=_pump_stdout, daemon=True).start()

            last_tick = started
            saw_event = False
            while True:
                now = time.monotonic()
                if now >= deadline:
                    _kill_process_group(proc)
                    raise subprocess.TimeoutExpired(cmd, EXEC_TIMEOUT)
                try:
                    line = lines_q.get(timeout=min(1.0, max(0.05, deadline - now)))
                except queue.Empty:
                    if on_progress and STREAM_INTERVAL > 0 and now - last_tick >= STREAM_INTERVAL:
                        last_tick = now
                        try:
                            on_progress(state, now - started)
                        except Exception:  # noqa: BLE001 - 提醒失败不中断执行
                            logging.warning("progress notify failed", exc_info=True)
                    continue
                if line is None:  # stdout EOF
                    break
                line = line.strip()
                if not line:
                    continue
                raw_lines.append(line)
                if line.startswith("{"):
                    try:
                        state.observe(json.loads(line))
                        saw_event = True
                    except (ValueError, TypeError):
                        pass  # 非 JSON 或结构异常的行忽略，不影响最终结果
                now = time.monotonic()
                if on_progress and STREAM_INTERVAL > 0 and now - last_tick >= STREAM_INTERVAL:
                    last_tick = now
                    try:
                        on_progress(state, now - started)
                    except Exception:  # noqa: BLE001 - 提醒失败不中断执行
                        logging.warning("progress notify failed", exc_info=True)
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # stdout 已 EOF 但进程没退（审计 P1：这里曾直接抛出，进程组
                # 没人清、_active_process 又被置空，留下失控子进程还回"已终止"）
                logging.error("claude 未随 stdout 关闭退出，终止进程组 pid=%s", proc.pid)
                _kill_process_group(proc)
                raise
            state.returncode = proc.returncode
            state.interrupt_requested = _interrupt_requested.is_set()
            err = "".join(err_buf).strip()
            logging.info(
                "claude exited rc=%s events=%s turns=%s interrupted=%s",
                proc.returncode,
                saw_event,
                state.turns,
                state.interrupted,
            )
        finally:
            with _process_lock:
                if _active_process is proc:
                    _active_process = None
            # 兜底（审计 P1）：任何异常退出路径都不许留下活着的子进程组
            if proc.poll() is None:
                logging.warning("异常路径下 claude 仍存活，终止进程组 pid=%s", proc.pid)
                _kill_process_group(proc)
        out = state.answer().strip()
        if not out and err:
            out = err[-1500:]  # 无 result 事件时回落到 stderr 摘要
        if proc.returncode != 0:
            # 会话文件被外部删除（如手动清理 ~/.claude/projects）：摘除映射重建一次
            if not is_new and session_id and "No conversation found" in err:
                logging.warning(
                    "claude session vanished; recreating (chat=%s)", chat_id or "?"
                )
                _chat_sessions.pop(chat_id, None)
                _persist_sessions()
                continue
            return f"⚠️ claude 执行失败 (rc={proc.returncode})\n```\n{err[:1500]}\n```", state
        if session_id:
            entry = _chat_sessions.get(chat_id)
            if entry is not None:
                entry["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _persist_sessions()
        return (out or "(无输出)"), state
    return "Service is stopping", RunState()


def _request_interrupt() -> bool:
    """终止当前执行的进程组；没有在跑的进程返回 False。

    特意绕开 _lock：执行期间 _execute_and_reply 一直持锁，打断消息必须能
    直接够到 _active_process，否则只能回一句"忙"。
    """
    with _process_lock:
        proc = _active_process
    if proc is None or proc.poll() is not None:
        return False
    # 先立标记再杀：CLI 会捕获 SIGTERM 并以 rc=143 正常退出，光看 returncode
    # 分不出"被我们打断"和"自己失败退出"。
    _interrupt_requested.set()
    logging.info("interrupt requested; killing process group pid=%s", proc.pid)
    _kill_process_group(proc)
    return True


def _is_interrupt(text: str) -> bool:
    return text.strip().lower() in INTERRUPT_WORDS


def shutdown() -> None:
    """Stop only the remote command group owned by this bridge."""
    _stopping.set()
    with _process_lock:
        proc = _active_process
    if proc is not None and proc.poll() is None:
        _kill_process_group(proc)


# ---------------------------------------------------------------------------
# 消息处理
# ---------------------------------------------------------------------------


def _strip_mention(data: P2ImMessageReceiveV1, text: str) -> str:
    """Remove mention placeholders using the exact mention keys (审计 M11)."""
    for mention in data.event.message.mentions or []:
        key = getattr(mention, "key", "") or ""
        if key:
            text = text.replace(key, "")
    # 兜底：@机器人显示名
    for marker in ("@Coder Claude Remote", "@Claude Remote"):
        text = text.replace(marker, "")
    return text.strip()


def _drain_queues() -> None:
    """唯一消费者线程：所有 chat 的排队指令都由它顺序执行。

    审计 P2：此前 _kick_queue 与收尾都能起消费者，而出队与获取执行锁不是原子
    的——两个消费者各弹一条后抢锁，先排的 A 可能后执行。现在全进程只允许一个
    消费者（_drain_running 在 _queue_lock 内判定与释放），出队即执行顺序。
    """
    global _drain_running
    while True:
        with _queue_lock:
            item = None
            for chat_id in list(_chat_queues):
                pending = _chat_queues.get(chat_id)
                if not pending:
                    continue
                item = (chat_id, pending.popleft())
                if pending:
                    # 还有剩余就轮转到队尾（dict 没有 move_to_end，重插即轮转）
                    _chat_queues.pop(chat_id)
                    _chat_queues[chat_id] = pending
                else:
                    _chat_queues.pop(chat_id, None)
                break
            if item is None:
                _drain_running = False  # 与"确认空队列"同锁，避免漏唤醒
                return
        chat_id, (item_data, item_text) = item
        _execute_and_reply(item_data, item_text, chat_id, queued=True)


def _kick_queue() -> None:
    """确保有消费者在跑；已有则什么都不做（空闲/忙时都成立）。"""
    global _drain_running
    with _queue_lock:
        if _drain_running:
            return
        _drain_running = True
    threading.Thread(target=_drain_queues, daemon=True).start()


def _queue_position(chat_id: str) -> int:
    with _queue_lock:
        return len(_chat_queues.get(chat_id) or ())


HELP_TEXT = (
    "🤖 **Claude Code 遥控**\n"
    "直接发指令即可执行（群聊需 @机器人，私聊直接发）。\n\n"
    "**命令**\n"
    "- `/new` 开新会话（清空当前上下文）\n"
    "- `/status` 会话状态（目录/上下文/队列/累计）\n"
    "- `/cd <目录>` 切换工作目录\n"
    "- `/cost` 本会话累计成本与轮次\n"
    "- `/queue <指令>` 排队执行（当前任务结束后自动开始）\n"
    "- `/help` 本帮助\n\n"
    "**执行中**\n"
    "- 发「停」「停止」「打断」「取消」「stop」「cancel」→ 打断当前任务\n"
    "- 其他消息会提示忙；想接着干活用 `/queue <指令>`\n"
    f"- 单条最长 {EXEC_TIMEOUT // 60} 分钟；进度卡片每 {STREAM_INTERVAL}s 原地刷新"
)


def _cmd_status(data: P2ImMessageReceiveV1, chat_id: str) -> None:
    entry = _chat_sessions.get(chat_id) or {}
    session_id = str(entry.get("session_id") or "")
    ctx_used = int(entry.get("ctx_used") or 0)
    if ctx_used and CONTEXT_WINDOW:
        ctx = f"{ctx_used / CONTEXT_WINDOW * 100:.0f}%（{ctx_used / 1000:.0f}k）"
    elif ctx_used:
        ctx = f"{ctx_used / 1000:.0f}k"
    else:
        ctx = "未知（本次会话还没跑过）"
    lines = [
        "📊 **会话状态**",
        f"- 会话：{session_id[:8] or '（未建立，下条消息新建）'}",
        f"- 目录：{_short_path(_chat_workdir(chat_id))}",
        f"- 上下文：{ctx}",
        f"- 队列：{_queue_position(chat_id)} 条待执行",
        f"- 累计：{int(entry.get('turns_total') or 0)} 轮 · ${float(entry.get('cost_usd') or 0):.4f} · "
        f"{float(entry.get('seconds_total') or 0) / 60:.1f}min",
    ]
    if entry.get("last_used"):
        lines.append(f"- 最近执行：{entry['last_used']}")
    _send_reply(data, "\n".join(lines))


def _cmd_cost(data: P2ImMessageReceiveV1, chat_id: str) -> None:
    entry = _chat_sessions.get(chat_id) or {}
    _send_reply(
        data,
        "💰 **本会话累计**\n"
        f"- 轮次：{int(entry.get('turns_total') or 0)}\n"
        f"- 成本：${float(entry.get('cost_usd') or 0):.4f}\n"
        f"- 执行时长：{float(entry.get('seconds_total') or 0) / 60:.1f}min",
    )


def _cmd_cd(data: P2ImMessageReceiveV1, chat_id: str, arg: str) -> None:
    if not arg:
        _send_reply(data, "用法：`/cd <目录>`（例：/cd ~/code/BEHAVIOR-1K）")
        return
    target = pathlib.Path(os.path.expanduser(arg))
    if not target.is_dir():
        _send_reply(data, f"❌ 目录不存在：{arg}")
        return
    resolved = str(target.resolve())
    with _sessions_lock:
        entry = _chat_entry(chat_id, create=True)
        entry["workdir"] = resolved
        _persist_sessions()
    _send_reply(data, f"📁 工作目录已切到 {_short_path(resolved)}")


def _cmd_new(data: P2ImMessageReceiveV1, chat_id: str) -> None:
    with _sessions_lock:
        entry = _chat_sessions.get(chat_id)
        if entry is not None:
            entry.pop("session_id", None)  # 保留 workdir 与累计统计
            _persist_sessions()
    with _queue_lock:
        _chat_queues.pop(chat_id, None)
    _send_reply(data, "🆕 已开新会话，下条消息从空上下文开始。")


def _cmd_queue(data: P2ImMessageReceiveV1, chat_id: str, arg: str) -> None:
    if not arg:
        _send_reply(data, "用法：`/queue <指令>`——当前任务结束后自动执行。")
        return
    with _queue_lock:
        pending = _chat_queues.setdefault(chat_id, deque())
        if len(pending) >= QUEUE_MAX:
            position = None
        else:
            pending.append((data, arg))
            position = len(pending)
    if position is None:
        _send_reply(data, f"❌ 队列已满（上限 {QUEUE_MAX} 条），等前面跑完再排。")
        return
    if _lock.locked():
        _send_reply(data, f"📋 已排队（第 {position} 位），当前任务结束后自动开始。")
    else:
        _send_reply(data, "▶️ 当前空闲，排队指令立即开始。")
    _kick_queue()


def _handle_command(data: P2ImMessageReceiveV1, text: str, chat_id: str) -> bool:
    """命令拦截；返回 True 表示已处理、不再进 claude。"""
    if not text.startswith("/"):
        return False
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/help", "/h", "/?"):
        _send_reply(data, HELP_TEXT)
    elif cmd in ("/new", "/clear"):
        _cmd_new(data, chat_id)
    elif cmd == "/status":
        _cmd_status(data, chat_id)
    elif cmd == "/cost":
        _cmd_cost(data, chat_id)
    elif cmd == "/cd":
        _cmd_cd(data, chat_id, arg)
    elif cmd in ("/queue", "/q"):
        _cmd_queue(data, chat_id, arg)
    else:
        _send_reply(data, f"未知命令 {cmd}，发 /help 看可用命令。")
    return True


def handle_message(data: P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    sender_info = data.event.sender
    sender = getattr(getattr(sender_info, "sender_id", None), "open_id", "") or ""
    sender_type = getattr(sender_info, "sender_type", "") or ""
    chat_type = msg.chat_type
    msg_id = msg.message_id or ""
    event_id = getattr(getattr(data, "header", None), "event_id", "") or ""
    fingerprint = _event_fingerprint(data)
    event_hash = hashlib.sha256(
        (event_id or msg_id or fingerprint).encode()
    ).hexdigest()[:10]

    # Persist before any reply or execution. Fail closed if state cannot be saved.
    try:
        is_new = _mark_seen(msg_id, event_id, fingerprint)
    except Exception as exc:  # noqa: BLE001
        logging.error(
            "seen state persist failed; event ignored hash=%s error=%s", event_hash, exc
        )
        return
    if not is_new:
        logging.info("dup event ignored hash=%s", event_hash)
        return

    stale_reason = _stale_event_reason(data)
    if stale_reason is not None:
        logging.warning(
            "stale event ignored hash=%s reason=%s", event_hash, stale_reason
        )
        return

    # Only human user messages may reach the command path. Never reply to rejected
    # senders, otherwise bot/app messages can create a feedback loop.
    if sender_type != "user":
        logging.warning(
            "non-user event ignored hash=%s sender_type=%s",
            event_hash,
            sender_type or "?",
        )
        return
    if sender not in ALLOW_OPEN_IDS:
        # sender 记入日志便于首次部署时收集本机用户 open_id（隐私：仅 open_id）
        logging.warning(
            "unauthorized event ignored hash=%s sender=%s", event_hash, sender
        )
        return
    logging.info("accepted event hash=%s chat_type=%s", event_hash, chat_type)

    # 群聊：只响应 @ 了机器人的消息
    if chat_type == "group":
        mentioned = {
            getattr(getattr(mention, "id", None), "open_id", "")
            for mention in (msg.mentions or [])
        }
        if BOT_OPEN_ID not in mentioned:
            return
    # 仅处理文本消息
    if msg.message_type != "text":
        return
    try:
        text = _strip_mention(data, json.loads(msg.content).get("text", ""))
    except Exception:  # noqa: BLE001
        return
    if not text:
        return
    # 日志脱敏（审计 H4）：不记录 prompt 原文
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    logging.info("prompt len=%d hash=%s", len(text), digest)

    if _handle_command(data, text, msg.chat_id or ""):
        return

    # 耗时执行必须放后台线程：WS 事件循环是单线程，同步阻塞会让 ping
    # 超时被服务器断连（3003 ping_timeout）
    threading.Thread(
        target=_execute_and_reply,
        args=(data, text, msg.chat_id or ""),
        daemon=True,
    ).start()


def _execute_and_reply(
    data: P2ImMessageReceiveV1, text: str, chat_id: str = "", *, queued: bool = False
) -> None:
    if queued:
        # 排队项：直接等锁（不抢跑也不回"忙"），轮到自己就执行
        _lock.acquire()
    elif not _lock.acquire(blocking=False):
        # 执行中：打断词直接终止进程组（不经过 _lock），其余消息回"忙"提示
        if _is_interrupt(text):
            if _request_interrupt():
                _send_reply(data, "🛑 已打断当前执行。")
            else:
                _send_reply(data, "（当前没有正在执行的指令）")
        elif not _send_reply(
            data, "⏳ 上一条指令还在执行。回复「停」可打断；想接续干活用 /queue <指令>。"
        ):
            logging.warning("busy reply send failed")
        return
    try:
        start = time.time()
        digest = hashlib.sha256(text.encode()).hexdigest()[:8]
        logging.info("exec start hash=%s", digest)
        # 立刻发一张卡片：既作"已收到"回执，也作为后续原地更新的载体。
        # 若等到第一个进度节流窗口才创建，短任务/慢启动会有 20s 无反馈。
        card_id: list[str | None] = [
            _post_card_reply(data, _card(RunState(), 0.0, starting=True))
        ]
        card_dead: list[bool] = [card_id[0] is None]
        if card_dead[0]:
            _send_reply(data, "🔄 收到，正在执行…（进度卡片不可用，完成后回复）")

        def _on_progress(state: RunState, elapsed_s: float) -> None:
            if STREAM_INTERVAL <= 0 or card_dead[0] or card_id[0] is None:
                return
            tick = time.monotonic()
            ok = _patch_card(card_id[0], _card(state, elapsed_s))
            # 记下每次刷新的耗时：刷新看着"变慢"时，要能区分是节流没触发、
            # 单次 PATCH 变慢，还是服务端限频（后者会静默不生效）。
            logging.info(
                "card tick at %.0fs patch=%.2fs ok=%s", elapsed_s, time.monotonic() - tick, ok
            )
            if not ok:
                card_dead[0] = True

        answer, state = _run_claude(text, chat_id, on_progress=_on_progress)
        elapsed = time.time() - start
        # 打断时用已产出的部分文本，而不是那句 rc 报错（结局由卡片标题表达，
        # 正文不再重复"已打断"）。
        if state.interrupted:
            answer = state.answer().strip() or "（打断时还没有产出内容）"
        logging.info(
            "exec done in %.0fs card=%s interrupted=%s", elapsed, bool(card_id[0]), state.interrupted
        )
        # 收尾一律用卡片：短任务（< 节流窗口）没有进度回调，此前会退化成纯文本。
        done_card = _card(state, elapsed, done=answer)
        delivered = False
        if card_id[0] is not None and not card_dead[0]:
            delivered = _patch_card(card_id[0], done_card)
        elif not card_dead[0]:
            delivered = _post_card_reply(data, done_card) is not None
        if not delivered:
            head = done_card["header"]["title"]["content"]  # 标题已含耗时
            prefix = f"{head}\n{state.statusline()}\n\n"
            if not _send_reply(data, f"{prefix}{answer}"):
                logging.warning("result reply send failed")
        elif len(answer) > CARD_BODY_MAX:
            # 卡片放不下的部分接着发（旧行为是整段丢弃，长报告会缺内容）
            if not _send_reply(data, f"（接上条卡片）\n{answer[CARD_BODY_MAX:]}"):
                logging.warning("overflow reply send failed")
        _update_chat_stats(chat_id, state, elapsed)
    except subprocess.TimeoutExpired:
        logging.error("exec timeout >%ss", EXEC_TIMEOUT)
        _send_reply(data, f"⏰ 执行超时（>{EXEC_TIMEOUT}s），进程组已终止。")
    except Exception as exc:  # noqa: BLE001
        logging.error("exec error: %s", exc)
        _send_reply(data, f"💥 内部错误：{exc}")
    finally:
        _lock.release()
        _kick_queue()  # 任意 chat 有积压都由唯一消费者接手；空队列时是空操作


class ObservedClient(ws.Client):
    async def _connect(self) -> None:
        # Keep SDK logging at WARNING: its INFO log includes the signed WS URL.
        await super()._connect()
        if self._conn is not None:
            logging.info("Feishu websocket connected")


def main() -> int:
    # 审计 C3：fail-closed——白名单为空且未显式开启开放模式时拒绝启动
    if not APP_ID or not APP_SECRET:
        print("FEISHU_APP_ID / FEISHU_APP_SECRET 未设置", flush=True)
        return 1
    if not ALLOW_OPEN_IDS and not OPEN_MODE:
        print(
            "FEISHU_ALLOW_OPEN_IDS 为空且未设置 FEISHU_OPEN_MODE=1，拒绝启动（安全默认）",
            flush=True,
        )
        return 1
    if not pathlib.Path(SETTINGS).is_file():
        print(f"遥控专用 settings 不存在: {SETTINGS}", flush=True)
        return 1
    event_handler = (
        EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(handle_message)
        .build()
    )
    ws_client = ObservedClient(
        APP_ID,
        APP_SECRET,
        event_handler=event_handler,
        log_level=LogLevel.WARNING,
    )
    logging.info(
        "bridge started (bot_configured=%s, allow=%d, open_mode=%s)",
        bool(BOT_OPEN_ID),
        len(ALLOW_OPEN_IDS),
        OPEN_MODE,
    )
    ws_client.start()  # 长连接，阻塞
    return 0


if __name__ == "__main__":
    sys.exit(main())
