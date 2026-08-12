from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync.py"
SPEC = importlib.util.spec_from_file_location("workspace_memory_sync", SCRIPT)
assert SPEC and SPEC.loader
SYNC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)


def daily(machine: str, agent: str, marker: str) -> dict:
    return {
        "schema_version": 1,
        "machine_id": machine,
        "agent": agent,
        "date": "2026-08-05",
        "events": [{"event_id": marker}],
    }


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / ".agents"
        self.root.mkdir()
        self.config = {
            "workspace": "code",
            "workspace_root": "~/code",
            "machine_id": "machine-a",
            "machine_role": "test",
            "shared_writer": True,
            "long_term_writer": True,
            "sync": {
                "enabled": True,
                "initialized": True,
                "remote": "bos/bucket/agent-memory-v2",
                "clear_proxy": True,
            },
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_daily(self, base: Path, machine: str, agent: str, marker: str) -> Path:
        path = base / machine / agent / "2026-08-05.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(daily(machine, agent, marker)), encoding="utf-8")
        return path

    def write_shared(self, base: Path, marker: str) -> None:
        files = {
            ".share/config/prompts/AGENTS.md": f"agents {marker}\n",
            ".share/config/prompts/CLAUDE.md": f"claude {marker}\n",
            ".share/long-term/_workspace.md": f"workspace {marker}\n",
            ".share/skills/memory/SKILL.md": f"skill {marker}\n",
        }
        for relative, content in files.items():
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def write_eval_docs(self, base: Path, request: str, report: str) -> None:
        files = {
            ".share/shared_files/b1k-docs/eval_request.yaml": request,
            ".share/shared_files/b1k-docs/eval_report.md": report,
        }
        for relative, content in files.items():
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def fake_download(self, source: Path):
        def download(_remote, destination, **_kwargs):
            SYNC.copy_shared_snapshot(source, Path(destination))

        return download

    def test_build_local_config_uses_safe_non_writer_default(self) -> None:
        template = {
            "schema_version": 2,
            "workspace": "workspace",
        }
        config = SYNC.build_local_config(
            template,
            remote="bos/bucket/agent-memory-v2",
            machine_id="machine-b",
            machine_role="training",
            workspace_root="~/code",
            shared_writer=False,
            clear_proxy=True,
        )
        self.assertEqual(config["machine_id"], "machine-b")
        self.assertFalse(config["shared_writer"])
        self.assertFalse(config["long_term_writer"])
        self.assertNotIn("projects", config)
        self.assertNotIn("secret", json.dumps(config).casefold())

    def test_push_memory_uploads_only_current_machine(self) -> None:
        self.write_daily(self.root / ".share" / "memory", "machine-a", "codex", "local")
        self.write_daily(self.root / ".share" / "memory", "machine-b", "claude", "cached")
        with mock.patch.object(SYNC, "mirror") as mirror:
            SYNC.push_memory(self.root, self.config, dry_run=False)
        source, target = mirror.call_args.args
        self.assertEqual(source, self.root / ".share" / "memory" / "machine-a")
        self.assertEqual(target, "bos/bucket/agent-memory-v2/memory/machine-a")

    def test_pull_memory_preserves_owned_prefix(self) -> None:
        local_owned = self.write_daily(self.root / ".share" / "memory", "machine-a", "codex", "local-new")
        remote = Path(self.temporary.name) / "remote-memory"
        self.write_daily(remote, "machine-a", "codex", "remote-stale")
        self.write_daily(remote, "machine-b", "claude", "remote-new")

        def fake_mirror(_source, target, **_kwargs):
            shutil.copytree(remote, Path(target), dirs_exist_ok=True)

        with (
            mock.patch.object(SYNC, "remote_has_objects", return_value=True),
            mock.patch.object(SYNC, "mirror", side_effect=fake_mirror),
        ):
            SYNC.pull_other_memory(self.root, self.config, dry_run=False)

        owned = json.loads(local_owned.read_text(encoding="utf-8"))
        other = json.loads(
            (self.root / ".share" / "memory" / "machine-b" / "claude" / "2026-08-05.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(owned["events"][0]["event_id"], "local-new")
        self.assertEqual(other["events"][0]["event_id"], "remote-new")

    def test_first_shared_pull_installs_remote_when_local_is_empty(self) -> None:
        remote = Path(self.temporary.name) / "remote-shared"
        self.write_shared(remote, "remote")
        with mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)):
            SYNC.reconcile_shared(
                self.root,
                self.config,
                mode="pull",
                dry_run=False,
                allow_non_writer=False,
            )
        self.assertEqual(
            (self.root / ".share" / "long-term" / "_workspace.md").read_text(encoding="utf-8"),
            "workspace remote\n",
        )
        self.assertTrue(SYNC.shared_base(self.root).is_dir())

    def test_first_shared_sync_detects_unbased_divergence(self) -> None:
        self.write_shared(self.root, "local")
        remote = Path(self.temporary.name) / "remote-shared"
        self.write_shared(remote, "remote")
        with (
            mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)),
            self.assertRaisesRegex(SYNC.SyncError, "尚无共同基线"),
        ):
            SYNC.reconcile_shared(
                self.root,
                self.config,
                mode="both",
                dry_run=False,
                allow_non_writer=False,
            )
        self.assertEqual(
            (self.root / ".share" / "long-term" / "_workspace.md").read_text(encoding="utf-8"),
            "workspace local\n",
        )
        self.assertTrue((SYNC.shared_conflict(self.root) / "report.json").is_file())
        self.assertEqual(
            (SYNC.shared_conflict(self.root) / "remote" / ".share" / "long-term" / "_workspace.md").read_text(
                encoding="utf-8"
            ),
            "workspace remote\n",
        )

    def test_pull_rejects_remote_regression_with_missing_files(self) -> None:
        """远端缺少本地已有的文件（旧版覆盖/删除特征）→ 拒绝拉取并保存快照。"""
        self.write_shared(self.root, "base")
        SYNC.update_shared_base(self.root, self.root)
        remote = Path(self.temporary.name) / "remote-shared"
        # 远端只有部分文件（缺 _workspace.md 与 CLAUDE.md → 回退特征）
        remote_files = {
            ".share/config/prompts/AGENTS.md": "agents remote\n",
            ".share/long-term/_workspace.md": "workspace base\n",
            ".share/skills/memory/SKILL.md": "skill base\n",
        }
        for relative, content in remote_files.items():
            path = remote / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        # 远端缺 CLAUDE.md（本地/基线有，未写入即缺失）

        with (
            mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)),
            self.assertRaisesRegex(SYNC.SyncError, "远端缺少本地已有的 shared 文件"),
        ):
            SYNC.reconcile_shared(
                self.root,
                self.config,
                mode="both",
                dry_run=False,
                allow_non_writer=False,
            )
        # 本地未被覆盖（CLAUDE.md 还在），冲突快照已保存
        self.assertTrue((self.root / ".share" / "config" / "prompts" / "CLAUDE.md").is_file())
        self.assertTrue((SYNC.shared_conflict(self.root) / "report.json").is_file())
        self.assertTrue(
            (SYNC.shared_conflict(self.root) / "remote" / ".share" / "config" / "prompts" / "CLAUDE.md").exists()
            is False
        )

    def test_pull_accepts_remote_forward_addition(self) -> None:
        """远端新增文件（向前变更）→ 正常拉取。"""
        self.write_shared(self.root, "base")
        SYNC.update_shared_base(self.root, self.root)
        remote = Path(self.temporary.name) / "remote-shared"
        self.write_shared(remote, "remote")
        extra = remote / ".share" / "shared_files" / "new_doc.md"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("new doc\n", encoding="utf-8")

        with mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)):
            SYNC.reconcile_shared(
                self.root,
                self.config,
                mode="both",
                dry_run=False,
                allow_non_writer=False,
            )
        self.assertEqual(
            (self.root / ".share" / "shared_files" / "new_doc.md").read_text(encoding="utf-8"),
            "new doc\n",
        )

    def test_remote_only_shared_change_is_pulled(self) -> None:
        self.write_shared(self.root, "base")
        SYNC.update_shared_base(self.root, self.root)
        remote = Path(self.temporary.name) / "remote-shared"
        self.write_shared(remote, "remote")
        with mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)):
            SYNC.reconcile_shared(
                self.root,
                self.config,
                mode="both",
                dry_run=False,
                allow_non_writer=False,
            )
        self.assertEqual(
            (self.root / ".share" / "skills" / "memory" / "SKILL.md").read_text(encoding="utf-8"),
            "skill remote\n",
        )

    def test_local_only_shared_change_is_uploaded_by_writer(self) -> None:
        self.write_shared(self.root, "base")
        SYNC.update_shared_base(self.root, self.root)
        remote = Path(self.temporary.name) / "remote-shared"
        self.write_shared(remote, "base")
        (self.root / ".share" / "skills" / "memory" / "SKILL.md").write_text(
            "skill local\n", encoding="utf-8"
        )
        with (
            mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)),
            mock.patch.object(SYNC, "upload_shared") as upload,
        ):
            SYNC.reconcile_shared(
                self.root,
                self.config,
                mode="push",
                dry_run=False,
                allow_non_writer=False,
            )
        upload.assert_called_once_with(self.root, self.config, dry_run=False)

    def test_non_writer_cannot_upload_local_shared_change(self) -> None:
        self.config["shared_writer"] = False
        self.config["long_term_writer"] = False
        self.write_shared(self.root, "base")
        SYNC.update_shared_base(self.root, self.root)
        remote = Path(self.temporary.name) / "remote-shared"
        self.write_shared(remote, "base")
        (self.root / ".share" / "skills" / "memory" / "SKILL.md").write_text(
            "skill local\n", encoding="utf-8"
        )
        with (
            mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)),
            self.assertRaisesRegex(SYNC.SyncError, "不是 shared_writer"),
        ):
            SYNC.reconcile_shared(
                self.root,
                self.config,
                mode="push",
                dry_run=False,
                allow_non_writer=False,
            )

    def test_both_sides_changed_requires_merge(self) -> None:
        self.write_shared(self.root, "base")
        SYNC.update_shared_base(self.root, self.root)
        (self.root / ".share" / "long-term" / "_workspace.md").write_text(
            "workspace local\n", encoding="utf-8"
        )
        remote = Path(self.temporary.name) / "remote-shared"
        self.write_shared(remote, "remote")
        with (
            mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)),
            self.assertRaisesRegex(SYNC.SyncError, "shared 分叉"),
        ):
            SYNC.reconcile_shared(
                self.root,
                self.config,
                mode="both",
                dry_run=False,
                allow_non_writer=False,
            )
        report = json.loads(
            (SYNC.shared_conflict(self.root) / "report.json").read_text(encoding="utf-8")
        )
        self.assertIn(".share/long-term/_workspace.md", report["local_changes"])
        self.assertIn(".share/long-term/_workspace.md", report["remote_changes"])

    def test_publish_shared_file_merges_remote_request_and_local_report(self) -> None:
        self.write_shared(self.root, "base")
        self.write_eval_docs(self.root, "request base\n", "report base\n")
        SYNC.update_shared_base(self.root, self.root)
        (self.root / ".share/shared_files/b1k-docs/eval_report.md").write_text(
            "report local\n", encoding="utf-8"
        )
        remote = Path(self.temporary.name) / "remote-file-publish"
        self.write_shared(remote, "base")
        self.write_eval_docs(remote, "request remote\n", "report base\n")

        with (
            mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)),
            mock.patch.object(SYNC, "copy_object") as copy,
        ):
            SYNC.publish_shared_file(
                self.root,
                self.config,
                Path("shared_files/b1k-docs/eval_report.md"),
                dry_run=False,
                allow_non_writer=False,
            )

        copy.assert_called_once()
        self.assertEqual(
            (self.root / ".share/shared_files/b1k-docs/eval_request.yaml").read_text(),
            "request remote\n",
        )
        self.assertEqual(
            (self.root / ".share/shared_files/b1k-docs/eval_report.md").read_text(),
            "report local\n",
        )

    def test_publish_shared_file_rejects_concurrent_report_change(self) -> None:
        self.write_shared(self.root, "base")
        self.write_eval_docs(self.root, "request base\n", "report base\n")
        SYNC.update_shared_base(self.root, self.root)
        (self.root / ".share/shared_files/b1k-docs/eval_report.md").write_text(
            "report local\n", encoding="utf-8"
        )
        remote = Path(self.temporary.name) / "remote-file-conflict"
        self.write_shared(remote, "base")
        self.write_eval_docs(remote, "request base\n", "report remote\n")

        with (
            mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)),
            self.assertRaisesRegex(SYNC.SyncError, "同一 shared 文件"),
        ):
            SYNC.publish_shared_file(
                self.root,
                self.config,
                Path("shared_files/b1k-docs/eval_report.md"),
                dry_run=False,
                allow_non_writer=False,
            )

    def test_resolve_refuses_unmerged_remote_additions(self) -> None:
        """resolve 前远端新增未并入本地（用户只合了部分冲突）→ 拒绝整包上传。"""
        self.write_shared(self.root, "base")
        SYNC.update_shared_base(self.root, self.root)
        (self.root / ".share" / "long-term" / "_workspace.md").write_text(
            "workspace local\n", encoding="utf-8"
        )
        remote = Path(self.temporary.name) / "remote-shared"
        self.write_shared(remote, "remote")
        # 远端新增一个文件（base 没有）——分叉前就存在，report 快照含它
        extra = remote / ".share" / "shared_files" / "new_doc.md"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("remote new\n", encoding="utf-8")
        with (
            mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)),
            self.assertRaises(SYNC.SyncError),
        ):
            SYNC.reconcile_shared(
                self.root,
                self.config,
                mode="both",
                dry_run=False,
                allow_non_writer=False,
            )
        # 本地未合并 new_doc.md，直接 resolve → 应被 H2 拒绝
        with (
            mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)),
            mock.patch.object(SYNC, "upload_shared") as upload,
            self.assertRaisesRegex(SYNC.SyncError, "请先合并远端新增文件"),
        ):
            SYNC.resolve_shared(self.root, self.config, dry_run=False, allow_non_writer=False)
        upload.assert_not_called()

    def test_resolve_refuses_when_remote_changed_again(self) -> None:
        self.write_shared(self.root, "base")
        SYNC.update_shared_base(self.root, self.root)
        (self.root / ".share" / "long-term" / "_workspace.md").write_text(
            "workspace local\n", encoding="utf-8"
        )
        remote = Path(self.temporary.name) / "remote-shared"
        self.write_shared(remote, "remote")
        with (
            mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)),
            self.assertRaises(SYNC.SyncError),
        ):
            SYNC.reconcile_shared(
                self.root,
                self.config,
                mode="both",
                dry_run=False,
                allow_non_writer=False,
            )
        self.write_shared(remote, "remote-new")
        with (
            mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)),
            mock.patch.object(SYNC, "upload_shared") as upload,
            self.assertRaisesRegex(SYNC.SyncError, "合并期间再次变化"),
        ):
            SYNC.resolve_shared(
                self.root,
                self.config,
                dry_run=False,
                allow_non_writer=False,
            )
        upload.assert_not_called()

    def test_init_pull_uses_git_template_and_installs_s3_shared(self) -> None:
        remote = Path(self.temporary.name) / "remote-shared"
        self.write_shared(remote, "remote")
        (self.root / "config.template.json").write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "workspace": "workspace",
                    "workspace_root": "~/code",
                }
            ),
            encoding="utf-8",
        )
        args = Namespace(
            push=False,
            pull=True,
            remote="bos/bucket/agent-memory-v2",
            machine_id="machine-b",
            machine_role="training",
            workspace_root="~/code",
            shared_writer=False,
            clear_proxy=True,
            reuse_machine=False,
            force=False,
            allow_non_writer=False,
            dry_run=False,
        )
        with (
            mock.patch.object(SYNC, "validate_control_plane"),
            mock.patch.object(SYNC, "remote_has_objects", return_value=False),
            mock.patch.object(SYNC, "download_shared", side_effect=self.fake_download(remote)),
            mock.patch.object(SYNC, "pull_other_memory"),
            mock.patch.object(SYNC, "configure_agent_links"),
            mock.patch.object(SYNC, "validate_local_memory"),
        ):
            result = SYNC.command_init(self.root, args)
        config = json.loads((self.root / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(result, 0)
        self.assertEqual(config["machine_id"], "machine-b")
        self.assertFalse(config["shared_writer"])
        self.assertTrue(config["sync"]["initialized"])
        self.assertNotIn("projects", config)
        self.assertEqual(
            (self.root / ".share" / "long-term" / "_workspace.md").read_text(encoding="utf-8"),
            "workspace remote\n",
        )

    def test_configure_agent_links_installs_every_shared_skill(self) -> None:
        home = Path(self.temporary.name) / "home"
        workspace = Path(self.temporary.name) / "workspace"
        self.config["workspace_root"] = str(workspace)
        # 2026-08-12：skills 分类布局（common/eval/memory/train），递归发现
        for category, names in (
            ("common", ("kimi-invoke",)),
            ("eval", ("analyze-b1k-eval", "sync-b1k-eval-request")),
            ("memory", ("check-memory", "memory", "write-work-report")),
            ("train", ("upload-pi-checkpoint",)),
        ):
            for name in names:
                skill = self.root / ".share" / "skills" / category / name
                skill.mkdir(parents=True, exist_ok=True)
                (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        prompts = self.root / ".share" / "config" / "prompts"
        prompts.mkdir(parents=True, exist_ok=True)
        (prompts / "AGENTS.md").write_text("agents\n", encoding="utf-8")
        (prompts / "CLAUDE.md").write_text("claude\n", encoding="utf-8")

        with mock.patch.object(SYNC.Path, "home", return_value=home):
            SYNC.configure_agent_links(self.root, self.config, dry_run=False)

        expected = {
            "kimi-invoke": "common/kimi-invoke",
            "analyze-b1k-eval": "eval/analyze-b1k-eval",
            "sync-b1k-eval-request": "eval/sync-b1k-eval-request",
            "check-memory": "memory/check-memory",
            "memory": "memory/memory",
            "write-work-report": "memory/write-work-report",
            "upload-pi-checkpoint": "train/upload-pi-checkpoint",
        }
        for agent_home in (".codex", ".claude", ".kimi-code"):
            for name, relative in expected.items():
                link = home / agent_home / "skills" / name
                self.assertTrue(link.is_symlink(), f"{link} missing for {name}")
                self.assertEqual(
                    link.resolve(),
                    (self.root / ".share" / "skills" / relative).resolve(),
                )
        self.assertEqual((workspace / "AGENTS.md").resolve(), (prompts / "AGENTS.md").resolve())
        self.assertEqual((workspace / "CLAUDE.md").resolve(), (prompts / "CLAUDE.md").resolve())

    def test_regular_sync_runs_raw_and_shared_paths(self) -> None:
        args = Namespace(dry_run=False)
        with (
            mock.patch.object(SYNC, "load_config", return_value=self.config),
            mock.patch.object(SYNC, "validate_local_memory"),
            mock.patch.object(SYNC, "push_memory") as push_memory,
            mock.patch.object(SYNC, "pull_other_memory") as pull_memory,
            mock.patch.object(SYNC, "reconcile_shared") as shared,
        ):
            result = SYNC.command_sync(self.root, args)
        self.assertEqual(result, 0)
        push_memory.assert_called_once_with(self.root, self.config, dry_run=False)
        pull_memory.assert_called_once_with(self.root, self.config, dry_run=False)
        shared.assert_called_once_with(
            self.root,
            self.config,
            mode="both",
            dry_run=False,
            allow_non_writer=False,
        )

    def test_cli_exposes_explicit_shared_workflow(self) -> None:
        parser = SYNC.parser()
        command_action = next(action for action in parser._actions if action.dest == "command")
        self.assertEqual(
            set(command_action.choices),
            {
                "init",
                "pull",
                "push",
                "push-memory",
                "sync",
                "pull-shared",
                "push-shared",
                "publish-shared-file",
                "sync-shared",
                "resolve-shared",
                "status",
            },
        )

    def test_regular_sync_is_blocked_before_initialization(self) -> None:
        config = dict(self.config)
        config["sync"] = dict(self.config["sync"], initialized=False)
        (self.root / "config.json").write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaisesRegex(SYNC.SyncError, "尚未初始化"):
            SYNC.load_config(self.root)
        loaded = SYNC.load_config(self.root, require_initialized=False)
        self.assertFalse(loaded["sync"]["initialized"])


if __name__ == "__main__":
    unittest.main()
