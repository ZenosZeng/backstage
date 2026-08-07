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
        for name in ("memory", "update-workspace-memory"):
            skill = self.root / ".share" / "skills" / name
            skill.mkdir(parents=True, exist_ok=True)
            (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        prompts = self.root / ".share" / "config" / "prompts"
        prompts.mkdir(parents=True, exist_ok=True)
        (prompts / "AGENTS.md").write_text("agents\n", encoding="utf-8")
        (prompts / "CLAUDE.md").write_text("claude\n", encoding="utf-8")

        with mock.patch.object(SYNC.Path, "home", return_value=home):
            SYNC.configure_agent_links(self.root, self.config, dry_run=False)

        for agent_home in (".codex", ".claude", ".kimi-code"):
            for name in ("memory", "update-workspace-memory"):
                link = home / agent_home / "skills" / name
                self.assertTrue(link.is_symlink())
                self.assertEqual(link.resolve(), (self.root / ".share" / "skills" / name).resolve())
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
