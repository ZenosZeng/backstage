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
import shutil
import subprocess
import threading
import urllib.request

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
_token_lock = threading.Lock()
_process_lock = threading.Lock()
_stopping = threading.Event()
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


def _persist_sessions() -> None:
    state_path = pathlib.Path(SESSION_FILE)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
    payload = {"version": 1, "chats": _chat_sessions}
    temp_path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    os.chmod(temp_path, 0o600)
    os.replace(temp_path, state_path)


def _get_or_create_session(chat_id: str) -> tuple[str | None, bool]:
    """返回该 chat 的 claude session_id 及是否新建。

    新建时先登记再执行（执行中途崩溃也不丢 id）；返回 (None, False) 表示不启用。
    """
    if not SESSION_CONTEXT_ENABLED or not chat_id:
        return None, False
    entry = _chat_sessions.get(chat_id)
    if entry and entry.get("session_id"):
        return str(entry["session_id"]), False
    session_id = str(uuid.uuid4())
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _chat_sessions[chat_id] = {
        "session_id": session_id,
        "created_at": now,
        "last_used": now,
    }
    _persist_sessions()
    return session_id, True


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


def _run_claude(prompt: str, chat_id: str = "") -> str:
    """Execute the prompt headless in a fresh process group; returns final text.

    会话上下文：同一 chat 复用 claude 原生会话（新建用 --session-id 固定 id，
    之后 -r 恢复），对话历史（含工具调用）由 claude 自己持久化并自动压缩。
    会话被外部清理（~/.claude/projects 被删）时自动重建一次。
    """
    global _active_process
    while not _stopping.is_set():
        session_id, is_new = _get_or_create_session(chat_id)
        cmd = [CLAUDE, "-p", prompt, "--max-turns", "200", "--settings", SETTINGS]
        if session_id:
            cmd += ["--session-id", session_id] if is_new else ["-r", session_id]
        with _process_lock:
            if _stopping.is_set():
                return "Service is stopping"
            proc = subprocess.Popen(
                cmd,
                cwd=WORKDIR,
                env=_build_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            _active_process = proc
        try:
            out, err = proc.communicate(timeout=EXEC_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            raise
        finally:
            with _process_lock:
                if _active_process is proc:
                    _active_process = None
        out = (out or "").strip()
        err = (err or "").strip()
        if proc.returncode != 0:
            # 会话文件被外部删除（如手动清理 ~/.claude/projects）：摘除映射重建一次
            if not is_new and session_id and "No conversation found" in err:
                logging.warning(
                    "claude session vanished; recreating (chat=%s)", chat_id or "?"
                )
                _chat_sessions.pop(chat_id, None)
                _persist_sessions()
                continue
            return f"⚠️ claude 执行失败 (rc={proc.returncode})\n```\n{err[:1500]}\n```"
        if session_id:
            entry = _chat_sessions.get(chat_id)
            if entry is not None:
                entry["last_used"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                _persist_sessions()
        return out or "(无输出)"
    return "Service is stopping"


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

    if text in ("/help", "help", "帮助"):
        _send_reply(
            data,
            "🤖 Claude Code 遥控\n"
            "- 直接发指令（如：检查训练状态）→ claude -p 执行并回复\n"
            "- 群聊需 @ 机器人；私聊直接发\n"
            "- 单条最长 5 分钟，超时自动中断\n"
            "- 同一会话保留上下文（跨消息记忆；FEISHU_SESSION_CONTEXT=0 关闭）",
        )
        return

    # 耗时执行必须放后台线程：WS 事件循环是单线程，同步阻塞会让 ping
    # 超时被服务器断连（3003 ping_timeout）
    threading.Thread(
        target=_execute_and_reply,
        args=(data, text, msg.chat_id or ""),
        daemon=True,
    ).start()


def _execute_and_reply(
    data: P2ImMessageReceiveV1, text: str, chat_id: str = ""
) -> None:
    if not _lock.acquire(blocking=False):
        if not _send_reply(data, "⏳ 上一条指令还在执行，稍后再试。"):
            logging.warning("busy reply send failed")
        return
    try:
        if not _send_reply(data, "🔄 收到，正在执行…"):
            logging.warning("ack reply send failed")
        start = time.time()
        digest = hashlib.sha256(text.encode()).hexdigest()[:8]
        logging.info("exec start hash=%s", digest)
        answer = _run_claude(text, chat_id)
        elapsed = time.time() - start
        logging.info("exec done in %.0fs", elapsed)
        if not _send_reply(data, f"✅ 执行完成（{elapsed:.0f}s）\n\n{answer}"):
            logging.warning("result reply send failed")
    except subprocess.TimeoutExpired:
        logging.error("exec timeout >%ss", EXEC_TIMEOUT)
        _send_reply(data, f"⏰ 执行超时（>{EXEC_TIMEOUT}s），进程组已终止。")
    except Exception as exc:  # noqa: BLE001
        logging.error("exec error: %s", exc)
        _send_reply(data, f"💥 内部错误：{exc}")
    finally:
        _lock.release()


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
