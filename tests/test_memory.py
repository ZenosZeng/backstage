from __future__ import annotations

import json
import datetime as dt
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "memory.py"


class MemoryCliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / ".agents"
        self.root.mkdir()
        workspace = Path(self.temporary.name) / "code"
        workspace.mkdir()
        (self.root / "config.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workspace": "code",
                    "workspace_root": str(workspace),
                    "machine_id": "test-machine",
                    "projects": {
                        "repo-a": {"path": "repo-a"},
                        "repo-b": {"path": "repo-b"},
                    },
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(self.root), *args],
            check=check,
            capture_output=True,
            text=True,
        )

    def add(self, title: str, *, agent: str = "codex") -> subprocess.CompletedProcess[str]:
        return self.run_cli(
            "add",
            "--agent",
            agent,
            "--project",
            "repo-a",
            "--task",
            "cross-repo-task",
            "--type",
            "fact",
            "--topic",
            "tests/concurrency",
            "--title",
            title,
            "--what",
            f"已确认事实：{title}",
            "--verified-by",
            "单元测试",
        )

    def test_workspace_event_does_not_require_project_registry(self) -> None:
        config_path = self.root / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config.pop("projects")
        config_path.write_text(json.dumps(config), encoding="utf-8")

        result = self.run_cli(
            "add",
            "--agent",
            "codex",
            "--type",
            "fact",
            "--topic",
            "workspace/config",
            "--title",
            "工作区事件",
            "--what",
            "无需项目注册表",
        )
        self.assertIn("已记录 1 条事件", result.stdout)

    def test_add_search_and_status(self) -> None:
        self.add("动作维度一致")
        search = self.run_cli("search", "动作维度", "--project", "repo-a")
        self.assertIn("动作维度一致", search.stdout)
        status = self.run_cli("status")
        self.assertIn("Events: 1", status.stdout)
        self.assertIn("'codex': 1", status.stdout)

    def test_same_agent_concurrent_writes_do_not_lose_events(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda index: self.add(f"并发事件 {index}"), range(24)))
        self.assertTrue(all(result.returncode == 0 for result in results))
        status = self.run_cli("status")
        self.assertIn("Events: 24", status.stdout)
        self.run_cli("validate")

    def test_agents_write_separate_daily_files(self) -> None:
        self.add("Codex 事件", agent="codex")
        self.add("Claude 事件", agent="claude")
        files = sorted((self.root / ".share" / "memory" / "test-machine").glob("*/*.json"))
        self.assertEqual([path.parent.name for path in files], ["claude", "codex"])

    def test_kimi_agent_can_add_event(self) -> None:
        self.add("Kimi 事件", agent="kimi")
        status = self.run_cli("status")
        self.assertIn("'kimi': 1", status.stdout)
        self.run_cli("validate")

    def test_import_is_idempotent_and_supports_multiple_projects(self) -> None:
        event = {
            "schema_version": 1,
            "event_id": "evt_import_test",
            "created_at": "2026-08-04T00:00:00.000000Z",
            "machine_id": "other-machine",
            "agent": "claude",
            "workspace": "code",
            "scope": "task",
            "task_id": "cross-repo-task",
            "projects": [{"name": "repo-a"}, {"name": "repo-b"}],
            "event_type": "decision",
            "topic_key": "tests/import",
            "title": "跨仓库决定",
            "content": {"what": "关联两个仓库"},
            "status": "active",
            "supersedes": [],
            "sensitivity": "normal",
        }
        source = Path(self.temporary.name) / "events.json"
        source.write_text(json.dumps([event], ensure_ascii=False), encoding="utf-8")
        first = self.run_cli("import", "--input", str(source))
        second = self.run_cli("import", "--input", str(source))
        self.assertIn("新增 1，重复 0", first.stdout)
        self.assertIn("新增 0，重复 1", second.stdout)
        result = self.run_cli("search", "跨仓库", "--project", "repo-b")
        self.assertIn("跨仓库决定", result.stdout)

    def test_hypothesis_is_marked_needs_verification(self) -> None:
        result = self.run_cli(
            "add",
            "--agent",
            "codex",
            "--type",
            "hypothesis",
            "--topic",
            "tests/hypothesis",
            "--title",
            "待验证假设",
            "--what",
            "可能存在配置问题",
        )
        self.assertEqual(result.returncode, 0)
        output = self.run_cli("search", "待验证假设", "--json")
        events = json.loads(output.stdout)
        self.assertEqual(events[0]["status"], "needs_verification")

    def test_secret_is_rejected_without_creating_event(self) -> None:
        source = Path(self.temporary.name) / "secret.json"
        source.write_text(
            json.dumps(
                [
                    {
                        "schema_version": 1,
                        "event_id": "evt_secret",
                        "created_at": "2026-08-04T00:00:00Z",
                        "machine_id": "other-machine",
                        "agent": "codex",
                        "workspace": "code",
                        "projects": [],
                        "event_type": "config",
                        "topic_key": "tests/secret",
                        "title": "错误事件",
                        "content": {"what": "不应写入", "token": "sensitive-value"},
                        "status": "active",
                    }
                ]
            ),
            encoding="utf-8",
        )
        result = self.run_cli("import", "--input", str(source), check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("疑似包含 secret", result.stderr)
        self.assertFalse((self.root / ".share" / "memory").exists())

    def sample_event(self, event_id: str = "original", **changes) -> dict:
        event = {
            "schema_version": 1, "event_id": event_id,
            "created_at": "2026-08-04T00:00:00Z", "machine_id": "other-machine",
            "agent": "claude", "workspace": "code", "projects": [{"name": "repo-a"}],
            "event_type": "fact", "topic_key": "tests/read", "title": "test",
            "content": {"what": "old conclusion", "verified_by": ["unit test"]},
            "status": "active", "supersedes": [],
        }
        event.update(changes)
        return event

    def import_events(self, events: list[dict], *, check=True):
        path = Path(self.temporary.name) / "events.json"
        path.write_text(json.dumps(events), encoding="utf-8")
        return self.run_cli("import", "--input", str(path), check=check)

    def test_conflicting_id_rejects_entire_import_without_writes(self):
        self.import_events([self.sample_event()])
        before = {str(p): p.read_bytes() for p in (self.root / ".share").rglob("*.json")}
        result = self.import_events([
            self.sample_event("new", created_at="2026-08-05T00:00:00Z"),
            self.sample_event(content={"what": "different"}),
        ], check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("冲突", result.stderr)
        self.assertEqual(before, {str(p): p.read_bytes() for p in (self.root / ".share").rglob("*.json")})

    def test_conflicting_id_in_other_date_is_rejected(self):
        self.import_events([self.sample_event()])
        result = self.import_events([self.sample_event(created_at="2026-08-05T00:00:00Z")], check=False)
        self.assertEqual(result.returncode, 2)

    def test_filters_and_utc_ordering(self):
        self.import_events([
            self.sample_event("a"),
            self.sample_event("b", machine_id="machine-b", created_at="2026-08-04T08:30:00+08:00"),
            self.sample_event("c", machine_id="machine-b", created_at="2026-08-04T02:00:00Z"),
            self.sample_event("d", machine_id="machine-b", created_at="2026-08-05T00:00:00Z"),
        ])
        result = self.run_cli("recent", "--machine", "machine-b", "--topic", "tests/read", "--since", "2026-08-04", "--until", "2026-08-05", "--json")
        self.assertEqual([e["event_id"] for e in json.loads(result.stdout)], ["c", "b"])
        self.assertEqual(self.run_cli("recent", "--since", "2026-08-05", "--until", "2026-08-04", check=False).returncode, 2)
        self.assertEqual(self.run_cli("recent", "--limit", "0", check=False).returncode, 2)

    def test_current_filters_superseded_before_search_and_keeps_parallel_claims(self):
        self.import_events([
            self.sample_event("old"),
            self.sample_event("new", agent="codex", supersedes=["old"], content={"what": "new result"}),
            self.sample_event("parallel", content={"what": "independent result"}),
        ])
        old = self.run_cli("search", "old conclusion", "--agent", "claude", "--current", "--json")
        self.assertEqual(json.loads(old.stdout), [])
        current = self.run_cli("recent", "--current", "--json")
        self.assertEqual({e["event_id"] for e in json.loads(current.stdout)}, {"new", "parallel"})
        self.assertEqual(len(json.loads(self.run_cli("recent", "--json").stdout)), 3)

    def test_get_and_audit_preserve_legacy_source(self):
        event = self.sample_event(created_at="2026-08-04T08:00:00+08:00")
        self.import_events([event])
        path = next((self.root / ".share").rglob("*.json"))
        document = json.loads(path.read_text())
        document["events"][0]["created_at"] = "2026-08-05T08:00:00+08:00"
        path.write_text(json.dumps(document))
        self.run_cli("validate")
        fetched = json.loads(self.run_cli("get", "original").stdout)
        self.assertTrue(fetched["source"].endswith("2026-08-04.json"))
        result = self.run_cli("audit", "--json", check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(set(json.loads(result.stdout)["counts"]), {"legacy_non_utc", "legacy_date_mismatch"})
        self.assertEqual(self.run_cli("get", "absent", check=False).returncode, 1)

    def test_audit_is_read_only_and_reports_unverified_stale_references(self):
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)).isoformat()
        self.import_events([self.sample_event(event_type="hypothesis", status="needs_verification", created_at=old, content={"what": "not tested"}, supersedes=["unknown"])])
        before = {str(p): p.read_bytes() for p in (self.root / ".share").rglob("*.json")}
        report = json.loads(self.run_cli("audit", "--json", check=False).stdout)
        self.assertEqual(report["counts"], {"missing_source": 1, "dangling_supersedes": 1, "stale_candidate": 1})
        self.assertEqual(before, {str(p): p.read_bytes() for p in (self.root / ".share").rglob("*.json")})

    def test_audit_preserves_parallel_superseding_decisions(self):
        self.import_events([self.sample_event("base"), self.sample_event("a", supersedes=["base"]), self.sample_event("b", supersedes=["base"])])
        report = json.loads(self.run_cli("audit", "--json", check=False).stdout)
        self.assertEqual(report["counts"], {"parallel_supersedes_candidate": 2})

    def test_invalid_schema_is_rejected(self):
        for changes in ({"schema_version": 2}, {"event_id": []}, {"supersedes": "id"}, {"supersedes": ["original"]}, {"created_at": "2026-08-04T00:00:00"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.import_events([self.sample_event(**changes)], check=False).returncode, 2)
        self.assertFalse((self.root / ".share" / "memory").exists())

    def test_daily_envelope_mismatch_is_rejected(self):
        self.import_events([self.sample_event()])
        path = next((self.root / ".share").rglob("*.json"))
        document = json.loads(path.read_text())
        document["machine_id"] = "wrong-machine"
        path.write_text(json.dumps(document))
        self.assertEqual(self.run_cli("validate", check=False).returncode, 1)

    def test_supersedes_cycle_is_visible_and_current_refuses_to_guess(self):
        self.import_events([self.sample_event("a", supersedes=["b"]), self.sample_event("b", supersedes=["a"])])
        report = json.loads(self.run_cli("audit", "--json", check=False).stdout)
        self.assertEqual(report["counts"], {"supersedes_cycle": 2})
        self.assertEqual(self.run_cli("recent", "--current", check=False).returncode, 2)
        self.assertEqual(len(json.loads(self.run_cli("recent", "--json").stdout)), 2)

    def test_plain_text_token_is_rejected_without_echo(self):
        token = "ghp_" + "A" * 36
        result = self.import_events([self.sample_event(token, content={"what": token})], check=False)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertFalse((self.root / ".share/memory").exists())

    def transition(self, state, *extra, check=True):
        return self.run_cli(
            "task", "--task", "cross-repo-task", "--state", state,
            "--agent", "codex", "--reason", "用户决定", "--where", "测试指令",
            *extra, check=check,
        )

    def brief(self, *extra):
        return json.loads(self.run_cli("brief", "--project", "repo-a", "--json", *extra).stdout)

    def lifecycle_event(self, event_id, state, **changes):
        return self.sample_event(
            event_id, event_type="decision", task_id="cross-repo-task",
            content={"task_state": state, "what": "决定依据", "where": ["user"]}, **changes,
        )

    def test_lifecycle_append_only_pause_close_reopen_and_old_cli_compatibility(self):
        self.add("保留原事实")
        self.import_events([self.sample_event(
            "todo", task_id="cross-repo-task", event_type="progress",
            content={"what": "正在做", "next": ["执行下一步"]},
        )])
        old = json.loads(self.run_cli("get", "todo").stdout)["event"]
        self.transition("paused")
        self.assertEqual(self.brief()["hidden_inactive_tasks"], 1)
        explicit = self.brief("--task", "cross-repo-task")
        self.assertEqual(explicit["tasks"][0]["state"], "paused")
        self.assertFalse(explicit["sections"]["待办候选"])
        self.transition("closed")
        self.add("普通进展不重开")
        self.assertEqual(self.brief("--include-inactive")["tasks"][0]["state"], "closed")
        self.assertFalse(self.brief("--include-inactive")["sections"]["待办候选"])
        self.transition("active")
        self.assertEqual(self.brief()["tasks"][0]["state"], "active")
        self.assertTrue(self.brief()["sections"]["待办候选"])
        self.assertEqual(json.loads(self.run_cli("get", "todo").stdout)["event"], old)
        self.assertIn("保留原事实", self.run_cli("search", "保留原事实", "--current").stdout)
        self.run_cli("validate")

    def test_task_creation_and_cross_repo_inherited_projects(self):
        self.assertEqual(self.transition("closed", check=False).returncode, 2)
        self.transition("active", "--project", "repo-a", "--project", "repo-b")
        self.transition("closed")
        other = json.loads(self.run_cli("brief", "--project", "repo-b", "--include-inactive", "--json").stdout)
        self.assertEqual(other["tasks"][0]["projects"], ["repo-a", "repo-b"])
        self.assertEqual(other["tasks"][0]["state"], "closed")

    def test_lifecycle_conflict_keeps_both_heads_and_requires_explicit_resolution(self):
        self.import_events([
            self.lifecycle_event("base", "active"),
            self.lifecycle_event("a", "paused", supersedes=["base"]),
            self.lifecycle_event("b", "closed", supersedes=["base"], machine_id="machine-b"),
            self.sample_event("todo", task_id="cross-repo-task", content={"what": "x", "next": ["不能推荐"]}),
        ])
        result = self.brief()
        self.assertEqual(result["tasks"][0]["state"], "conflict")
        self.assertEqual({e["event_id"] for e in result["tasks"][0]["heads"]}, {"a", "b"})
        self.assertFalse(result["sections"]["待办候选"])
        self.assertEqual(self.transition("active", check=False).returncode, 2)
        self.assertEqual(self.transition("active", "--resolve", "a", check=False).returncode, 2)
        self.transition("active", "--resolve", "a", "--resolve", "b")
        self.assertEqual(self.brief()["tasks"][0]["state"], "active")

    def test_local_concurrent_transitions_are_serialized(self):
        self.add("起点")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda state: self.transition(state), ["active", "paused", "active", "closed"]))
        self.assertTrue(all(r.returncode == 0 for r in results))
        task = self.brief("--include-inactive")["tasks"][0]
        self.assertNotEqual(task["state"], "conflict")
        self.assertEqual(len(task["heads"]), 1)
        self.assertEqual(len(json.loads(self.run_cli("recent", "--json").stdout)), 5)

    def test_task_state_validation_and_reference_failures(self):
        for changes in (
            {"task_id": ""}, {"event_type": "fact"},
            {"content": {"task_state": "invalid", "what": "x", "where": ["y"]}},
            {"content": {"task_state": "closed", "what": "", "where": []}},
        ):
            event = self.lifecycle_event("bad", "active")
            event.update(changes)
            self.assertEqual(self.import_events([event], check=False).returncode, 2)
        self.import_events([self.lifecycle_event("dangling", "closed", supersedes=["absent"])])
        self.assertEqual(self.run_cli("brief", "--project", "repo-a", check=False).returncode, 2)

    def test_brief_reads_global_lifecycle_before_date_and_agent_filters(self):
        self.import_events([
            self.sample_event("old", task_id="cross-repo-task", content={"next": ["过期待办"]}),
            self.lifecycle_event("close", "closed", agent="codex", created_at="2026-08-05T00:00:00Z"),
        ])
        report = self.brief("--agent", "claude", "--until", "2026-08-05")
        self.assertFalse(report["sections"]["待办候选"])
        self.assertEqual(report["hidden_inactive_tasks"], 1)

    def test_search_relevance_beats_new_incidental_mention_and_can_restore_recent(self):
        self.import_events([
            self.sample_event("relevant", title="B1K 当前结果"),
            self.sample_event("incidental", title="GOAI 路演", created_at="2026-08-05T00:00:00Z",
                              content={"what": "顺带提到B1K"}),
        ])
        ranked = json.loads(self.run_cli("search", "b1k", "--json").stdout)
        self.assertEqual([e["event_id"] for e in ranked], ["relevant", "incidental"])
        recent = json.loads(self.run_cli("search", "b1k", "--sort", "recent", "--json").stdout)
        self.assertEqual(recent[0]["event_id"], "incidental")
        self.assertFalse(json.loads(self.run_cli("search", "b1k missing", "--json").stdout))

    def test_search_short_output_full_escape_and_json_compatibility(self):
        body = "中文内容" * 200 + "完整结尾"
        self.import_events([self.sample_event(content={"what": body})])
        short = self.run_cli("search", "test", "--summary-chars", "40").stdout
        self.assertNotIn("完整结尾", short)
        self.assertIn("original", short)
        self.assertIn("完整结尾", self.run_cli("search", "test", "--full").stdout)
        self.assertEqual(json.loads(self.run_cli("search", "test", "--json").stdout)[0]["content"]["what"], body)
        self.assertNotIn("完整结尾", self.run_cli("recent", "--summary").stdout)
        self.assertIn("完整结尾", self.run_cli("recent").stdout)

    def test_brief_deduplicates_exact_content_but_preserves_sources_and_parallel_claims(self):
        self.import_events([
            self.sample_event("a", task_id="cross-repo-task"),
            self.sample_event("b", task_id="cross-repo-task", agent="codex"),
            self.sample_event("different", task_id="cross-repo-task", content={"what": "相反结论"}),
            self.sample_event("hyp", event_type="hypothesis", status="needs_verification", content={"what": "待验证"}),
        ])
        report = self.brief()
        facts = report["sections"]["事实与验证记录"]
        self.assertEqual(len(facts), 2)
        self.assertTrue(any(set(e["sources"]) == {"a", "b"} for e in facts))
        self.assertEqual(len(report["sections"]["风险与待验证"]), 1)
        self.assertEqual(report["tasks"][0]["state"], "unknown")

    def test_brief_requires_scope_bounds_output_and_never_writes(self):
        self.assertEqual(self.run_cli("brief", check=False).returncode, 2)
        self.add("很多信息" * 100)
        before = {str(p): p.read_bytes() for p in (self.root / ".share").rglob("*.json")}
        result = self.run_cli("brief", "--project", "repo-a", "--max-chars", "200")
        self.assertLessEqual(len(result.stdout.rstrip("\n")), 200)
        self.assertIn("截断", result.stdout)
        self.assertEqual(before, {str(p): p.read_bytes() for p in (self.root / ".share").rglob("*.json")})
        self.assertEqual(self.run_cli("brief", "--task", "x", "--max-chars", "0", check=False).returncode, 2)

    def test_brief_keeps_older_constraints_despite_many_new_progress_events(self):
        self.import_events([
            self.sample_event("constraint", event_type="decision", content={"what": "禁止发射"}),
            *[self.sample_event(f"progress{i}", event_type="progress", title=f"进展{i}",
                               content={"what": str(i)}, created_at="2026-08-05T00:00:00Z") for i in range(25)],
        ])
        report = self.brief("--limit", "4")
        self.assertEqual(report["sections"]["约定与决策"][0]["sources"], ["constraint"])
        self.assertEqual(report["omitted_events"], 22)

    def test_cross_task_lifecycle_reference_is_not_a_valid_close(self):
        other = self.lifecycle_event("other", "active")
        other["task_id"] = "another-task"
        self.import_events([other, self.lifecycle_event("close", "closed", supersedes=["other"])])
        self.assertEqual(self.run_cli("brief", "--project", "repo-a", check=False).returncode, 2)

    def test_task_does_not_mutate_on_invalid_or_sensitive_input(self):
        self.add("existing")
        before = {str(p): p.read_bytes() for p in (self.root / ".share").rglob("*.json")}
        self.assertEqual(self.transition("closed", "--reason", "  ", check=False).returncode, 2)
        token = "ghp_" + "A" * 36
        result = self.transition("closed", "--reason", token, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertNotIn(token, result.stdout + result.stderr)
        self.assertEqual(before, {str(p): p.read_bytes() for p in (self.root / ".share").rglob("*.json")})


if __name__ == "__main__":
    unittest.main()
