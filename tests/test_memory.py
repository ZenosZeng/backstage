from __future__ import annotations

import json
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
        files = sorted((self.root / "memory" / "test-machine").glob("*/*.json"))
        self.assertEqual([path.parent.name for path in files], ["claude", "codex"])

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
        self.assertFalse((self.root / "memory").exists())


if __name__ == "__main__":
    unittest.main()
