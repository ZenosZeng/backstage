#!/usr/bin/env python3
"""Offline replay-guard regression tests; no Feishu or Claude calls."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1] / "claude_feishu"


def _event(*, message_id="m1", event_id="e1", created_at_ms=None, sender_type="user"):
    created = str(
        created_at_ms if created_at_ms is not None else int(time.time() * 1000)
    )
    message = SimpleNamespace(
        message_id=message_id,
        create_time=created,
        chat_id="chat",
        chat_type="p2p",
        message_type="text",
        content=json.dumps({"text": "status"}),
        mentions=[],
    )
    sender = SimpleNamespace(
        sender_id=SimpleNamespace(open_id="allowed"),
        sender_type=sender_type,
    )
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id, create_time=created),
        event=SimpleNamespace(message=message, sender=sender),
    )


class SessionContextTests(unittest.TestCase):
    """会话上下文：_execute_and_reply 必须把 chat_id 转发给 _run_claude。"""

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.sessions = Path(cls.tempdir.name) / "sessions.json"
        os.environ["CLAUDE_FEISHU_SESSIONS"] = str(cls.sessions)
        os.environ["CLAUDE_FEISHU_STATE"] = str(Path(cls.tempdir.name) / "seen.json")
        os.environ["FEISHU_ALLOW_OPEN_IDS"] = "allowed"
        spec = importlib.util.spec_from_file_location(
            "bridge_session_under_test", ROOT / "bridge.py"
        )
        cls.bridge = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.bridge)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def test_execute_and_reply_forwards_chat_id(self):
        """回归：bridge.py:524 曾漏传 chat_id，导致会话上下文永不生效。"""
        bridge = self.bridge
        with (
            mock.patch.object(bridge, "_send_reply", return_value=True),
            mock.patch.object(bridge, "_run_claude", return_value="ok") as run,
        ):
            bridge._execute_and_reply(
                _event(message_id="s1", event_id="se1"), "hello", "chat-123"
            )
        run.assert_called_once()
        self.assertEqual(run.call_args.args, ("hello", "chat-123"))
        self.assertIsNotNone(run.call_args.kwargs.get("on_progress"))

    def test_get_or_create_session_persists_mapping(self):
        bridge = self.bridge
        sid1, new1 = bridge._get_or_create_session("chat-a")
        sid2, new2 = bridge._get_or_create_session("chat-a")
        sid3, new3 = bridge._get_or_create_session("chat-b")
        self.assertTrue(new1)
        self.assertFalse(new2)
        self.assertTrue(new3)
        self.assertEqual(sid1, sid2)
        self.assertNotEqual(sid1, sid3)
        self.assertTrue(self.sessions.exists())
        self.assertEqual(self.sessions.stat().st_mode & 0o777, 0o600)
        payload = json.loads(self.sessions.read_text())
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["chats"]["chat-a"]["session_id"], sid1)

    def test_session_disabled_returns_none(self):
        bridge = self.bridge
        saved = bridge.SESSION_CONTEXT_ENABLED
        bridge.SESSION_CONTEXT_ENABLED = False
        try:
            self.assertEqual(bridge._get_or_create_session("chat-c"), (None, False))
        finally:
            bridge.SESSION_CONTEXT_ENABLED = saved


class ReplayGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tempdir.name) / "seen.json"
        cls.state.write_text(json.dumps(["legacy-message"]))
        os.environ["CLAUDE_FEISHU_STATE"] = str(cls.state)
        os.environ["FEISHU_ALLOW_OPEN_IDS"] = "allowed"
        spec = importlib.util.spec_from_file_location(
            "bridge_under_test", ROOT / "bridge.py"
        )
        cls.bridge = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.bridge)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def test_legacy_state_and_all_three_dedup_keys(self):
        bridge = self.bridge
        self.assertFalse(
            bridge._mark_seen("legacy-message", "new-event", "new-fingerprint")
        )
        self.assertTrue(bridge._mark_seen("m1", "e1", "f1"))
        self.assertFalse(bridge._mark_seen("m1", "e2", "f2"))
        self.assertFalse(bridge._mark_seen("m2", "e1", "f2"))
        self.assertFalse(bridge._mark_seen("m2", "e2", "f1"))
        state = json.loads(self.state.read_text())
        self.assertEqual(state["version"], 2)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o600)

    def test_stale_and_fresh_event_classification(self):
        bridge = self.bridge
        old = _event(created_at_ms=bridge._STARTED_AT_MS - 60_000)
        fresh = _event(created_at_ms=int(time.time() * 1000))
        self.assertIsNotNone(bridge._stale_event_reason(old))
        self.assertIsNone(bridge._stale_event_reason(fresh))

    def test_rejected_events_never_execute_or_reply(self):
        bridge = self.bridge
        old = _event(
            message_id="old",
            event_id="old-event",
            created_at_ms=bridge._STARTED_AT_MS - 60_000,
        )
        app = _event(message_id="app", event_id="app-event", sender_type="app")
        with (
            mock.patch.object(bridge, "_send_reply") as reply,
            mock.patch.object(bridge.threading, "Thread") as thread,
        ):
            bridge.handle_message(old)
            bridge.handle_message(app)
        reply.assert_not_called()
        thread.assert_not_called()


class RunClaudeProgressTests(unittest.TestCase):
    """长任务执行：按 PROGRESS_INTERVAL 分片等待并回调 on_progress，总时长受 EXEC_TIMEOUT 约束。"""

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_FEISHU_SESSIONS"] = str(Path(cls.tempdir.name) / "sessions.json")
        os.environ["CLAUDE_FEISHU_STATE"] = str(Path(cls.tempdir.name) / "seen.json")
        os.environ["FEISHU_ALLOW_OPEN_IDS"] = "allowed"
        os.environ["CLAUDE_FEISHU_TIMEOUT"] = "3600"
        os.environ["CLAUDE_FEISHU_PROGRESS_INTERVAL"] = "600"
        spec = importlib.util.spec_from_file_location(
            "bridge_progress_under_test", ROOT / "bridge.py"
        )
        cls.bridge = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.bridge)

    def test_heartbeat_until_completion(self):
        bridge = self.bridge

        class FakeProc:
            returncode = 0

            def __init__(self):
                self.chunks = []

            def communicate(self, timeout=None):
                self.chunks.append(timeout)
                if len(self.chunks) < 3:
                    raise subprocess.TimeoutExpired(["claude"], timeout)
                return "done", ""

            def poll(self):
                return None

        fake = FakeProc()
        progress = []
        with (
            mock.patch.object(bridge, "_get_or_create_session", return_value=("sid", False)),
            mock.patch.object(bridge, "_persist_sessions"),
            mock.patch.object(bridge.subprocess, "Popen", return_value=fake),
        ):
            out = bridge._run_claude("p", "chat", on_progress=progress.append)
        self.assertEqual(out, "done")
        self.assertEqual(len(progress), 2)
        self.assertEqual(fake.chunks, [600.0, 600.0, 600.0])

    def test_total_deadline_kills_process_group(self):
        bridge = self.bridge

        class NeverFinishes:
            def communicate(self, timeout=None):
                raise subprocess.TimeoutExpired(["claude"], timeout)

            def poll(self):
                return None

        with (
            mock.patch.object(bridge, "_get_or_create_session", return_value=("sid", False)),
            mock.patch.object(bridge, "_persist_sessions"),
            mock.patch.object(bridge.subprocess, "Popen", return_value=NeverFinishes()),
            mock.patch.object(bridge, "_kill_process_group") as kill,
            mock.patch.object(bridge.time, "monotonic", side_effect=[1000.0, 4600.0]),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                bridge._run_claude("p", "chat")
        kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
