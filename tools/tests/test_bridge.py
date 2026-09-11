#!/usr/bin/env python3
"""Offline replay-guard regression tests; no Feishu or Claude calls."""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
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
            mock.patch.object(bridge, "_run_claude",
                              return_value=("ok", bridge.RunState())) as run,
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

    def _stream_proc(self, lines, rc=0, stderr_lines=(), delay=0.0, block_after=False):
        """构造一个 stream-json 形态的假子进程。

        delay: 每行之间的真实延迟——节流类断言需要真实时间流逝才会触发。
        block_after: 产出完不 EOF 而是继续阻塞，用于触发超时路径。
        """

        class FakeProc:
            def __init__(self):
                self.returncode = None
                self.pid = 4242
                self.stdout = self._gen()
                self.stderr = iter([line + "\n" for line in stderr_lines])

            def _gen(self):
                for line in lines:
                    if delay:
                        time.sleep(delay)
                    yield line + "\n"
                if block_after:
                    while True:
                        time.sleep(0.05)

            def wait(self, timeout=None):
                self.returncode = rc
                return rc

            def poll(self):
                return self.returncode

        return FakeProc()

    def test_streams_progress_and_reads_result_event(self):
        """逐行读事件：按节流回调进度，最终回答取 result 事件而非 stdout 原文。"""
        bridge = self.bridge
        events = [
            json.dumps({"type": "system", "subtype": "init", "model": "m-1"}),
            json.dumps({"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 128}),
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read"}]}}),
            json.dumps({"type": "result", "subtype": "success", "result": "最终答案",
                        "num_turns": 3, "total_cost_usd": 0.25,
                        "usage": {"input_tokens": 1000, "output_tokens": 50}}),
        ]
        fake = self._stream_proc(events, delay=0.06)
        progress = []
        # 节流窗口设成极小值，用真实时间驱动回调。**不要 mock time.monotonic**：
        # bridge.time 就是全局 time 模块，改它会连 queue.Queue 内部的超时计算
        # 一起改掉，导致队列永不返回（实测把测试挂死）。
        with (
            mock.patch.object(bridge, "_get_or_create_session", return_value=("sid", False)),
            mock.patch.object(bridge, "_persist_sessions"),
            mock.patch.object(bridge.subprocess, "Popen", return_value=fake),
            mock.patch.object(bridge, "STREAM_INTERVAL", 0.05),
        ):
            out, state = bridge._run_claude("p", "chat", on_progress=lambda s, e: progress.append((s, e)))
        self.assertEqual(out, "最终答案")
        self.assertGreaterEqual(len(progress), 1)
        self.assertEqual(state.model, "m-1")
        self.assertEqual(state.thinking, 128)
        self.assertEqual(state.turns, 3)
        self.assertEqual(state.cost, 0.25)
        self.assertIn("m-1", state.statusline())

    def test_progress_shows_context_from_assistant_events(self):
        """回归：usage 只从 result 事件取的话，执行中的卡片永远显示不出 ctx。

        result 要整轮跑完才发，而 5s 刷新的进度卡片恰恰是在执行中看的——
        必须从每条 assistant 事件的 message.usage 实时取。
        """
        bridge = self.bridge
        usage = {
            "input_tokens": 800,
            "cache_read_input_tokens": 136400,
            "cache_creation_input_tokens": 0,
            "output_tokens": 1100,
        }
        events = [
            json.dumps({"type": "system", "subtype": "init", "model": "m-1"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "干活中"}], "usage": usage},
                }
            ),
        ]
        fake = self._stream_proc(events)
        with (
            mock.patch.object(bridge, "_get_or_create_session", return_value=("sid", False)),
            mock.patch.object(bridge, "_persist_sessions"),
            mock.patch.object(bridge.subprocess, "Popen", return_value=fake),
        ):
            _, state = bridge._run_claude("p", "chat")
        self.assertEqual(state.context_used(), 137200)
        # ctx 在标题栏（副标题），正文不再重复
        self.assertIn("ctx", state.statusline())
        self.assertNotIn("ctx", state.progress_md(5.0))

    def test_progress_shows_thinking_and_tool_details(self):
        """回归：思考正文与工具参数要进进度卡片——只写"调用 Bash"看不出在干什么。"""
        bridge = self.bridge
        state = bridge.RunState()
        state.observe(
            {"type": "system", "subtype": "thinking_tokens", "estimated_tokens": 2400}
        )
        state.observe({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "先看 observe 实现。\n\n再取工具参数。"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x/bridge.py"}},
        ]}})
        state.observe({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}},
        ]}})
        body = state.progress_md(65.0)
        self.assertIn("💭 先看 observe 实现。 再取工具参数。", body)  # 换行压平
        self.assertIn("- 当前：Bash: pytest -q", body)
        self.assertIn("1. Read: /x/bridge.py", body)  # 历史步骤
        self.assertEqual(body.count("Bash: pytest -q"), 1)  # 当前不进历史、不重复
        self.assertIn("思考 2.4k", body)

    def test_thinking_empty_block_keeps_previous_text(self):
        """redacted/空 thinking 块不能把上一段正文清掉。"""
        bridge = self.bridge
        state = bridge.RunState()
        state.observe({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "第一段"}]}})
        state.observe({"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": ""}]}})
        self.assertEqual(state.thinking_text, "第一段")

    def test_tool_summary_falls_back_and_caps_length(self):
        bridge = self.bridge
        self.assertEqual(bridge._tool_summary({"name": "Bash", "input": {}}), "Bash")
        self.assertEqual(
            bridge._tool_summary({"name": "Custom", "input": {"a": 1}}), 'Custom: {"a": 1}'
        )
        long_cmd = "x" * 500
        summary = bridge._tool_summary({"name": "Bash", "input": {"command": long_cmd}})
        self.assertLessEqual(len(summary), len("Bash: ") + bridge._TOOL_DETAIL_MAX + 1)
        self.assertTrue(summary.endswith("…"))

    def test_steps_history_is_capped(self):
        bridge = self.bridge
        state = bridge.RunState()
        for i in range(bridge.STEPS_MAX + 10):
            state.observe({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": f"/f{i}"}}]}})
        self.assertEqual(len(state.steps), bridge.STEPS_MAX)
        self.assertIn(f"/f{bridge.STEPS_MAX + 9}", state.steps[-1])

    def test_card_titles_carry_duration(self):
        """回归：耗时进主标题（执行中/执行完成都带），正文不重复。"""
        bridge = self.bridge
        state = bridge.RunState()
        state.observe({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "干活"}]}})
        running = bridge._card(state, 65.0)
        self.assertEqual(running["header"]["title"]["content"], "🤖 执行中 · 1min05s")
        self.assertNotIn("1min05s", running["elements"][0]["text"]["content"])
        done = bridge._card(state, 133.0, done="答案")
        self.assertEqual(done["header"]["title"]["content"], "✅ 执行完成 · 2min13s")
        state.returncode = 1
        self.assertIn("· 2min13s", bridge._card(state, 133.0, done="x")["header"]["title"]["content"])

    def test_fmt_duration_uses_min_and_seconds(self):
        bridge = self.bridge
        self.assertEqual(bridge._fmt_duration(45), "45s")
        self.assertEqual(bridge._fmt_duration(65), "1min05s")
        self.assertEqual(bridge._fmt_duration(3600), "60min00s")

    def test_context_ignores_result_cumulative_usage(self):
        """回归：result 的 usage 是整轮各次调用之和（实测 19998+182+9268=29448），
        拿它算 ctx 会随调用次数虚高——占比必须以最后一次 assistant 事件为准。"""
        bridge = self.bridge
        state = bridge.RunState()
        state.observe({"type": "assistant", "message": {
            "content": [{"type": "text", "text": "干活"}],
            "usage": {"input_tokens": 182, "cache_read_input_tokens": 20352},
        }})
        self.assertEqual(state.context_used(), 20534)
        state.observe({"type": "result", "result": "完了", "usage": {
            "input_tokens": 29448, "cache_read_input_tokens": 40960,
        }})
        self.assertEqual(state.context_used(), 20534)  # 不被累计值污染
        self.assertEqual(state.total_usage["input_tokens"], 29448)  # 累计量另存

    def test_statusline_carries_header_fields(self):
        """标题栏 = 模型+effort · 工作路径 · 上下文占比（不写机器名，手机端要短）。"""
        bridge = self.bridge
        state = bridge.RunState()
        state.model = "deepseek-flash"
        state.usage = {"input_tokens": 100, "cache_read_input_tokens": 199900}
        line = state.statusline()
        expected_model = f"deepseek-flash {bridge.EFFORT}".strip()
        self.assertTrue(line.startswith(expected_model), line)
        self.assertIn(bridge._short_path(bridge.WORKDIR), line)
        self.assertNotIn("effort", line)  # effort 不加前缀词，直接跟在模型后
        self.assertNotIn(socket.gethostname(), line)
        self.assertTrue(line.endswith("ctx 20%"), line)

    def test_short_path_trims_home_only(self):
        bridge = self.bridge
        self.assertEqual(
            bridge._short_path(str(bridge.HOME_DIR / "code")), "~/code"
        )
        self.assertEqual(bridge._short_path("/opt/other"), "/opt/other")

    def test_card_header_carries_statusline_as_subtitle(self):
        """statusline 在 header.subtitle，底部不再挂 note 脚注。"""
        bridge = self.bridge
        state = bridge.RunState()
        state.model = "deepseek-flash"
        state.usage = {"input_tokens": 100, "cache_read_input_tokens": 199900}
        card = bridge._card(state, 3.0, done="答案")
        self.assertEqual(card["header"]["subtitle"]["content"], state.statusline())
        self.assertEqual([e["tag"] for e in card["elements"]], ["div"])
        progress = bridge._card(state, 3.0)
        self.assertEqual(progress["header"]["subtitle"]["content"], state.statusline())

    def test_card_falls_back_to_no_subtitle_when_rejected(self):
        """回归：服务端不认 header.subtitle 时去掉重试，而不是整张卡片发不出去。"""
        bridge = self.bridge
        state = bridge.RunState()
        state.model = "m"
        card = bridge._card(state, 1.0, done="答案")
        seen = []

        def fake_api(_url, sent, _method):
            seen.append("subtitle" in (sent.get("header") or {}))
            body = {"code": 0, "data": {"message_id": "om_1"}} if not seen[-1] else {"code": 10002}
            return (not seen[-1]), body

        with (
            mock.patch.object(bridge, "_subtitle_supported", True),
            mock.patch.object(bridge, "_card_api", side_effect=fake_api),
        ):
            ok, _ = bridge._call_card_api("u", card, "POST")
            self.assertTrue(ok)
            self.assertFalse(bridge._subtitle_supported)
            # 之后构建的卡片不再带副标题（mock 退出会还原全局，故断言放在块内）
            self.assertNotIn("subtitle", bridge._card(state, 1.0, done="答案")["header"])
        self.assertEqual(seen, [True, False])  # 先带副标题，被拒后去掉重试
        self.assertEqual(bridge._without_subtitle(card)["header"].keys(), {"template", "title"})

    def test_card_header_reflects_outcome(self):
        """回归：打断/失败曾一律顶着绿色的「✅ 执行完成」标题。"""
        bridge = self.bridge

        def header(rc):
            state = bridge.RunState()
            state.returncode = rc
            card = bridge._card(state, 1.0, done="正文")
            return card["header"]["template"], card["header"]["title"]["content"]

        self.assertEqual(header(0)[0], "green")
        self.assertEqual(header(1)[0], "red")
        self.assertIn("rc=1", header(1)[1])
        stop_template, stop_title = header(-signal.SIGTERM)
        self.assertEqual(stop_template, "orange")
        self.assertIn("已打断", stop_title)
        self.assertEqual(
            bridge._card(bridge.RunState(), 1.0)["header"]["template"], "blue"
        )

    def test_interrupt_marks_state_and_skips_error_text(self):
        """被 SIGTERM 打死（rc=-15）时按打断处理，用已产出的文本而非 rc 报错。"""
        bridge = self.bridge
        events = [
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": "被打断前的部分输出"}]}}),
        ]
        fake = self._stream_proc(events, rc=-signal.SIGTERM)
        with (
            mock.patch.object(bridge, "_get_or_create_session", return_value=("sid", False)),
            mock.patch.object(bridge, "_persist_sessions"),
            mock.patch.object(bridge.subprocess, "Popen", return_value=fake),
        ):
            out, state = bridge._run_claude("p", "chat")
        self.assertTrue(state.interrupted)
        self.assertEqual(state.answer(), "被打断前的部分输出")

    def test_total_deadline_kills_process_group(self):
        """总时长超过 EXEC_TIMEOUT 时杀掉进程组并抛出超时。"""
        bridge = self.bridge
        events = [json.dumps({"type": "system", "subtype": "init", "model": "m"})]
        fake = self._stream_proc(events, block_after=True)
        with (
            mock.patch.object(bridge, "_get_or_create_session", return_value=("sid", False)),
            mock.patch.object(bridge, "_persist_sessions"),
            mock.patch.object(bridge.subprocess, "Popen", return_value=fake),
            mock.patch.object(bridge, "_kill_process_group",
                              side_effect=lambda p: setattr(p, "returncode", -15)) as kill,
            mock.patch.object(bridge, "EXEC_TIMEOUT", 0.3),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                bridge._run_claude("p", "chat")
        kill.assert_called_once()

    def test_hung_process_without_output_still_hits_deadline(self):
        """回归：子进程完全不产出时也必须能撞上 EXEC_TIMEOUT。

        旧实现把超时检查写在 `for line in proc.stdout` 循环体内，claude 挂住
        不产出就会永久阻塞、超时永不触发（与评测侧观察到的 sim 挂死同型）。
        现在读线程 + 队列，超时判定不依赖是否有输出。
        """
        bridge = self.bridge

        class _NeverYields:
            """永不产出下一项的阻塞迭代器。

            注意别写成「没有 yield 的生成器函数」——那种函数不是生成器，
            会在构造时就直接执行 while True 把测试自己挂死。
            """

            def __iter__(self):
                return self

            def __next__(self):
                while True:
                    time.sleep(0.05)

        class Hung:
            pid = 9
            returncode = None
            stderr = iter([])

            def __init__(self):
                self.stdout = _NeverYields()

            def poll(self):
                return self.returncode  # 被杀后（side_effect 置 -15）不再算存活

            def wait(self, timeout=None):
                return 0

        with (
            mock.patch.object(bridge, "_get_or_create_session", return_value=("sid", False)),
            mock.patch.object(bridge, "_persist_sessions"),
            mock.patch.object(bridge.subprocess, "Popen", return_value=Hung()),
            mock.patch.object(bridge, "_kill_process_group",
                              side_effect=lambda p: setattr(p, "returncode", -15)) as kill,
            mock.patch.object(bridge, "EXEC_TIMEOUT", 0.3),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                bridge._run_claude("p", "chat")
        kill.assert_called_once()

    def test_interrupt_detected_for_cli_rc143(self):
        """回归：CLI 捕获 SIGTERM 后以 143 正常退出，只认 -15 会漏判。

        实测日志：请求了打断、claude exited rc=143，却记成 interrupted=False，
        于是卡片顶着绿色「✅ 执行完成」、并拿 rc 报错当正文。
        """
        bridge = self.bridge

        def state(rc, flag=False):
            st = bridge.RunState()
            st.returncode = rc
            st.interrupt_requested = flag
            return st

        self.assertTrue(state(143).interrupted)
        self.assertTrue(state(-signal.SIGTERM).interrupted)
        self.assertFalse(state(0).interrupted)
        self.assertFalse(state(1).interrupted)
        # 显式标记优先：rc=0 也算打断
        self.assertTrue(state(0, flag=True).interrupted)

    def test_request_interrupt_sets_flag_for_run_state(self):
        """打断标记必须跨 _request_interrupt 传到本轮 RunState。"""
        bridge = self.bridge

        class Running:
            pid = 778

            def poll(self):
                return None

        bridge._interrupt_requested.clear()
        with (
            mock.patch.object(bridge, "_active_process", Running()),
            mock.patch.object(bridge, "_kill_process_group",
                              side_effect=lambda p: setattr(p, "returncode", -15)),
        ):
            self.assertTrue(bridge._request_interrupt())
        self.assertTrue(bridge._interrupt_requested.is_set())

    def test_request_interrupt_kills_active_process(self):
        """打断不走 _lock：只要能拿到 _active_process 就终止其进程组。"""
        bridge = self.bridge

        class Running:
            pid = 777

            def poll(self):
                return None

        with (
            mock.patch.object(bridge, "_active_process", Running()),
            mock.patch.object(bridge, "_kill_process_group",
                              side_effect=lambda p: setattr(p, "returncode", -15)) as kill,
        ):
            self.assertTrue(bridge._request_interrupt())
        kill.assert_called_once()

    def test_request_interrupt_noop_without_process(self):
        bridge = self.bridge
        with mock.patch.object(bridge, "_active_process", None):
            self.assertFalse(bridge._request_interrupt())


if __name__ == "__main__":
    unittest.main()


class RichOutputTests(unittest.TestCase):
    """工具结果与流式正文：卡片信息量向终端看齐。"""

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_FEISHU_SESSIONS"] = str(Path(cls.tempdir.name) / "sessions.json")
        os.environ["CLAUDE_FEISHU_STATE"] = str(Path(cls.tempdir.name) / "seen.json")
        os.environ["FEISHU_ALLOW_OPEN_IDS"] = "allowed"
        spec = importlib.util.spec_from_file_location("bridge_rich_under_test", ROOT / "bridge.py")
        cls.bridge = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.bridge)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def _delta(self, state, kind, text):
        key = "text" if kind == "text_delta" else "thinking"
        state.observe({"type": "stream_event", "event": {
            "type": "content_block_delta", "delta": {"type": kind, key: text}}})

    def test_tool_result_shows_in_progress_card(self):
        """回归：工具输出此前整条丢弃，卡片上只有命令、看不到结果。"""
        bridge = self.bridge
        state = bridge.RunState()
        state.observe({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "step 190000 loss 0.021", "is_error": False}]}})
        body = state.progress_md(10.0)
        self.assertIn("**最新输出**", body)
        self.assertIn("step 190000 loss 0.021", body)

    def test_tool_result_accepts_block_list_and_marks_error(self):
        bridge = self.bridge
        state = bridge.RunState()
        state.observe({"type": "user", "message": {"content": [
            {"type": "tool_result", "is_error": True,
             "content": [{"type": "text", "text": "command not found"}]}]}})
        self.assertTrue(state.last_result_error)
        body = state.progress_md(10.0)
        self.assertIn("⚠️", body)
        self.assertIn("command not found", body)

    def test_streaming_text_tail_is_shown(self):
        bridge = self.bridge
        state = bridge.RunState()
        state.observe({"type": "stream_event", "event": {"type": "content_block_start",
                                                         "content_block": {"type": "text"}}})
        for i in range(200):
            self._delta(state, "text_delta", f"字{i}")
        out = state.latest_output()
        self.assertTrue(out.startswith("…"), out)
        self.assertIn("字199", out)
        self.assertLessEqual(len(out), bridge.OUTPUT_MAX + 1)

    def test_streaming_thinking_is_shown(self):
        bridge = self.bridge
        state = bridge.RunState()
        self._delta(state, "thinking_delta", "先看日志再下结论")
        self.assertIn("先看日志再下结论", state.progress_md(1.0))

    def test_answer_falls_back_to_streamed_text(self):
        """只有 partial 事件（没有完整 assistant/result）时不能回空。"""
        bridge = self.bridge
        state = bridge.RunState()
        self._delta(state, "text_delta", "边写边发的正文")
        self.assertEqual(state.answer(), "边写边发的正文")

    def test_long_answer_overflow_is_sent_as_followup(self):
        """回归：卡片正文超过上限的部分此前直接丢弃，长报告会缺内容。"""
        bridge = self.bridge
        long_answer = "x" * (bridge.CARD_BODY_MAX + 128)
        state = bridge.RunState()
        state.returncode = 0
        sent = []
        with (
            mock.patch.object(bridge, "_run_claude", return_value=(long_answer, state)),
            mock.patch.object(bridge, "_post_card_reply", return_value="mid-1"),
            mock.patch.object(bridge, "_patch_card", return_value=True),
            mock.patch.object(bridge, "_persist_sessions"),
            mock.patch.object(bridge, "_send_reply",
                              side_effect=lambda d, t: sent.append(t) or True),
        ):
            bridge._execute_and_reply(_event(), "干活", "chat")
        self.assertEqual(len(sent), 1)
        self.assertIn("（接上条卡片）", sent[0])
        self.assertIn("x" * 100, sent[0])

    def test_done_card_note_carries_usage(self):
        bridge = self.bridge
        state = bridge.RunState()
        state.observe({"type": "result", "result": "完", "num_turns": 3,
                       "total_cost_usd": 0.0523, "usage": {"output_tokens": 2400}})
        card = bridge._card(state, 100.0, done="答案")
        note = card["elements"][1]["elements"][0]["content"]
        self.assertIn("3 轮", note)
        self.assertIn("2.4k tok", note)
        self.assertIn("$0.0523", note)


class CommandTests(unittest.TestCase):
    """命令集：/help /new /status /cost /cd /queue。"""

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_FEISHU_SESSIONS"] = str(Path(cls.tempdir.name) / "sessions.json")
        os.environ["CLAUDE_FEISHU_STATE"] = str(Path(cls.tempdir.name) / "seen.json")
        os.environ["FEISHU_ALLOW_OPEN_IDS"] = "allowed"
        spec = importlib.util.spec_from_file_location("bridge_cmd_under_test", ROOT / "bridge.py")
        cls.bridge = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.bridge)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        bridge = self.bridge
        bridge._chat_sessions.clear()
        bridge._chat_queues.clear()
        bridge._drain_running = False

    def _run(self, text, chat_id="chat"):
        bridge = self.bridge
        with mock.patch.object(bridge, "_send_reply", return_value=True) as send:
            handled = bridge._handle_command(_event(), text, chat_id)
        return handled, (send.call_args.args[1] if send.call_args else "")

    def test_plain_text_is_not_a_command(self):
        handled, _ = self._run("检查训练状态")
        self.assertFalse(handled)

    def test_help_lists_all_commands_and_interrupt_words(self):
        handled, reply = self._run("/help")
        self.assertTrue(handled)
        for token in ("/new", "/status", "/cd", "/cost", "/queue", "/help", "stop", "停"):
            self.assertIn(token, reply)

    def test_unknown_command_points_to_help(self):
        handled, reply = self._run("/frobnicate")
        self.assertTrue(handled)
        self.assertIn("/help", reply)

    def test_new_resets_session_but_keeps_workdir(self):
        bridge = self.bridge
        bridge._chat_sessions["chat"] = {"session_id": "sid-1", "workdir": "/tmp"}
        handled, _ = self._run("/new")
        self.assertTrue(handled)
        self.assertNotIn("session_id", bridge._chat_sessions["chat"])
        self.assertEqual(bridge._chat_sessions["chat"]["workdir"], "/tmp")

    def test_cd_sets_workdir_and_rejects_missing(self):
        bridge = self.bridge
        handled, reply = self._run("/cd /tmp")
        self.assertTrue(handled)
        self.assertEqual(bridge._chat_workdir("chat"), "/tmp")
        self.assertIn("/tmp", reply)
        _, reply = self._run("/cd /definitely/not/here")
        self.assertIn("不存在", reply)
        self.assertEqual(bridge._chat_workdir("chat"), "/tmp")  # 未改

    def test_workdir_defaults_to_global(self):
        bridge = self.bridge
        self.assertEqual(bridge._chat_workdir("nosuch"), bridge.WORKDIR)

    def test_session_creation_preserves_workdir(self):
        """回归：新建会话时整体覆盖 entry 会丢掉 /cd 设的目录。"""
        bridge = self.bridge
        with mock.patch.object(bridge, "_persist_sessions"):
            bridge._chat_sessions["chat"] = {"workdir": "/tmp"}
            bridge._get_or_create_session("chat")
        self.assertTrue(bridge._chat_sessions["chat"].get("session_id"))
        self.assertEqual(bridge._chat_sessions["chat"]["workdir"], "/tmp")

    def test_queue_reports_position_and_caps(self):
        """上限按"还在队列里的条数"算：消费者取走等锁的那条不再占额度。"""
        bridge = self.bridge
        with mock.patch.object(bridge, "_kick_queue"):  # 不真起消费者，只测计数
            bridge._lock.acquire()  # 模拟执行中
            try:
                _, reply = self._run("/queue 第一条")
                self.assertIn("第 1 位", reply)
                for i in range(1, bridge.QUEUE_MAX):
                    self._run(f"/queue 第{i+1}条")
                _, reply = self._run("/queue 溢出")
                self.assertIn("队列已满", reply)
                self.assertEqual(bridge._queue_position("chat"), bridge.QUEUE_MAX)
            finally:
                bridge._lock.release()

    def test_queue_when_idle_starts_immediately(self):
        bridge = self.bridge
        started = []

        class InlineThread:  # 就地执行，避免断言与线程启动竞态
            def __init__(self, target=None, args=(), daemon=None):
                self._target, self._args = target, args

            def start(self):
                self._target(*self._args)

        with (mock.patch.object(bridge, "_drain_queues",
                                side_effect=lambda: started.append("drain")),
              mock.patch.object(bridge.threading, "Thread", InlineThread)):
            _, reply = self._run("/queue 干活")
        self.assertIn("立即开始", reply)
        self.assertEqual(started, ["drain"])

    def test_status_and_cost_render_accumulated_stats(self):
        bridge = self.bridge
        bridge._chat_sessions["chat"] = {
            "session_id": "abcdefgh-1234", "workdir": "/tmp",
            "cost_usd": 0.25, "turns_total": 7, "seconds_total": 120.0, "ctx_used": 200000,
        }
        _, status = self._run("/status")
        self.assertIn("abcdefgh", status)
        self.assertIn("7 轮", status)
        self.assertIn("$0.2500", status)
        _, cost = self._run("/cost")
        self.assertIn("$0.2500", cost)
        self.assertIn("2.0min", cost)

    def test_drain_executes_queued_items_in_order(self):
        bridge = self.bridge
        order = []
        bridge._chat_queues["chat"] = __import__("collections").deque(
            [(_event(), "第一条"), (_event(), "第二条")]
        )
        with mock.patch.object(bridge, "_execute_and_reply",
                               side_effect=lambda d, t, c, queued=False: order.append(t)):
            bridge._drain_queues()
        self.assertEqual(order, ["第一条", "第二条"])
        self.assertEqual(bridge._queue_position("chat"), 0)


class AuditFixTests(unittest.TestCase):
    """2026-09-11 审计三项：异常路径子进程清理 / 跨 chat 排队唤醒 / 会话落盘并发。"""

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_FEISHU_SESSIONS"] = str(Path(cls.tempdir.name) / "sessions.json")
        os.environ["CLAUDE_FEISHU_STATE"] = str(Path(cls.tempdir.name) / "seen.json")
        os.environ["FEISHU_ALLOW_OPEN_IDS"] = "allowed"
        spec = importlib.util.spec_from_file_location("bridge_audit_under_test", ROOT / "bridge.py")
        cls.bridge = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.bridge)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        self.bridge._chat_sessions.clear()
        self.bridge._chat_queues.clear()

    # --- P1：异常退出路径不能留下活着的子进程组 ---
    def test_wait_timeout_kills_group_before_raising(self):
        """stdout 已 EOF 但进程没退：必须清进程组，再抛（而不是留孤儿）。"""
        bridge = self.bridge

        class Stubborn:
            pid = 4321
            returncode = None

            def __init__(self):
                self.stdout = iter([])  # 立刻 EOF
                self.stderr = iter([])

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired(["claude"], timeout)

        killed = []

        def fake_kill(proc):
            killed.append(proc.pid)
            proc.returncode = -15

        with (
            mock.patch.object(bridge, "_get_or_create_session", return_value=("sid", False)),
            mock.patch.object(bridge, "_persist_sessions"),
            mock.patch.object(bridge.subprocess, "Popen", return_value=Stubborn()),
            mock.patch.object(bridge, "_kill_process_group", side_effect=fake_kill),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                bridge._run_claude("p", "chat")
        self.assertEqual(killed, [4321])

    def test_exception_path_kills_surviving_process(self):
        """循环内异常（如事件解析炸弹）也不能把子进程丢在后台。"""
        bridge = self.bridge

        class Boom:
            pid = 4322
            returncode = None

            def __init__(self):
                self.stdout = iter(['{"type":"assistant"}\n'])  # 合法 JSON → 走到 observe
                self.stderr = iter([])

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return 0

        killed = []

        def fake_kill(proc):
            killed.append(proc.pid)
            proc.returncode = -15

        with (
            mock.patch.object(bridge, "_get_or_create_session", return_value=("sid", False)),
            mock.patch.object(bridge, "_persist_sessions"),
            mock.patch.object(bridge.subprocess, "Popen", return_value=Boom()),
            mock.patch.object(bridge, "_kill_process_group", side_effect=fake_kill),
            mock.patch.object(bridge.RunState, "observe", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(RuntimeError):
                bridge._run_claude("p", "chat")
        self.assertEqual(killed, [4322])

    # --- P2：跨 chat 排队唤醒 ---
    def test_queued_item_from_other_chat_gets_drained(self):
        """回归：A 执行中 B 用 /queue，A 结束只查自己的队列 → B 永远不执行。"""
        bridge = self.bridge
        order = []
        bridge._chat_queues["chat-b"] = __import__("collections").deque([(_event(), "B 的活")])
        with mock.patch.object(bridge, "_execute_and_reply",
                               side_effect=lambda d, t, c, queued=False: order.append((c, t))):
            bridge._drain_queues()
        self.assertEqual(order, [("chat-b", "B 的活")])
        self.assertEqual(bridge._queue_position("chat-b"), 0)

    def test_drain_round_robins_between_chats(self):
        """同一 chat 多条不能饿死另一个 chat。"""
        bridge = self.bridge
        from collections import deque
        bridge._chat_queues["a"] = deque([(_event(), "a1"), (_event(), "a2")])
        bridge._chat_queues["b"] = deque([(_event(), "b1")])
        order = []
        with mock.patch.object(bridge, "_execute_and_reply",
                               side_effect=lambda d, t, c, queued=False: order.append(t)):
            bridge._drain_queues()
        self.assertEqual(order[0], "a1")
        self.assertIn("b1", order[:2])  # b 不被 a 的两条压在后面

    # --- P2：会话落盘并发 ---
    def test_concurrent_persist_does_not_collide(self):
        """回归：/cd 与执行线程共用同一个临时文件名 → FileNotFoundError。"""
        bridge = self.bridge
        errors = []
        barrier = threading.Barrier(4)

        def worker(n):
            try:
                barrier.wait(timeout=5)
                for i in range(15):
                    with bridge._sessions_lock:
                        bridge._chat_sessions[f"c{n}"] = {"session_id": f"s{n}-{i}"}
                        bridge._persist_sessions()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(errors, [])
        payload = json.loads(Path(bridge.SESSION_FILE).read_text())
        self.assertEqual(len(payload["chats"]), 4)

    def test_persist_temp_name_is_unique_per_thread(self):
        bridge = self.bridge
        seen = []
        original = bridge.os.replace
        try:
            def spy(src, dst):
                seen.append(str(src))
                return original(src, dst)
            bridge.os.replace = spy
            bridge._persist_sessions()
        finally:
            bridge.os.replace = original
        self.assertTrue(seen and str(threading.get_ident()) in seen[0], seen)


class QueueFifoTests(unittest.TestCase):
    """审计复审 P2：出队与取执行锁曾非原子，先排的 A 可能后执行。"""

    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_FEISHU_SESSIONS"] = str(Path(cls.tempdir.name) / "sessions.json")
        os.environ["CLAUDE_FEISHU_STATE"] = str(Path(cls.tempdir.name) / "seen.json")
        os.environ["FEISHU_ALLOW_OPEN_IDS"] = "allowed"
        spec = importlib.util.spec_from_file_location("bridge_fifo_under_test", ROOT / "bridge.py")
        cls.bridge = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.bridge)

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    def setUp(self):
        bridge = self.bridge
        bridge._chat_sessions.clear()
        bridge._chat_queues.clear()
        bridge._drain_running = False

    def test_fifo_order_under_concurrent_kicks(self):
        """多个 kicker 同时抢也不能乱序：出队顺序 = 执行顺序。"""
        bridge = self.bridge
        from collections import deque
        total = 8
        bridge._chat_queues["chat"] = deque(
            [(_event(), f"n{i}") for i in range(total)]
        )
        order: list[str] = []
        order_lock = threading.Lock()

        def fake_exec(_d, t, _c, queued=False):
            with order_lock:
                order.append(t)
            time.sleep(0.005)  # 拉大并发窗口

        with mock.patch.object(bridge, "_execute_and_reply", side_effect=fake_exec):
            kickers = [threading.Thread(target=bridge._kick_queue) for _ in range(4)]
            for t in kickers:
                t.start()
            for t in kickers:
                t.join(timeout=5)
            deadline = time.time() + 10
            while time.time() < deadline:
                with order_lock:
                    if len(order) >= total:
                        break
                time.sleep(0.01)

        self.assertEqual(order, [f"n{i}" for i in range(total)])

    def test_only_one_consumer_thread_is_started(self):
        """并发 kick 只允许拉起一个消费者线程。"""
        bridge = self.bridge
        created = []

        class RecordingThread:
            def __init__(self, target=None, args=(), daemon=None):
                created.append(target)

            def start(self):
                pass  # 只记录，不真跑（消费者由测试自己控制）

        try:
            with mock.patch.object(bridge.threading, "Thread", RecordingThread):
                for _ in range(5):
                    bridge._kick_queue()
            self.assertEqual(len(created), 1)
        finally:
            bridge._drain_running = False  # 别把标志留给后续用例

    def test_empty_queue_releases_consumer_slot(self):
        """消费者在锁内确认队列空后才释放单消费者标志（避免漏唤醒）。"""
        bridge = self.bridge
        bridge._kick_queue()
        deadline = time.time() + 5
        while bridge._drain_running and time.time() < deadline:
            time.sleep(0.01)
        self.assertFalse(bridge._drain_running)

    def test_enqueue_after_consumer_exits_restarts_it(self):
        """消费者退出后新入队仍会被拉起（空队列与入队不会互相漏掉）。"""
        bridge = self.bridge
        from collections import deque
        order = []
        with mock.patch.object(bridge, "_execute_and_reply",
                               side_effect=lambda d, t, c, queued=False: order.append(t)):
            bridge._kick_queue()
            deadline = time.time() + 5
            while bridge._drain_running and time.time() < deadline:
                time.sleep(0.01)
            bridge._chat_queues["chat"] = deque([(_event(), "迟到的一条")])
            bridge._kick_queue()
            deadline = time.time() + 5
            while not order and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual(order, ["迟到的一条"])
