"""独立副本、命令副作用合入、冲突保护与整批撤回。"""

from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from chatcode.config import Settings
from chatcode.files import FileError
from chatcode.isolation import IsolatedTasks
from chatcode.tools import ToolContext, ToolError, call_tool
from chatcode.workspace import Workspace


class IsolationCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="chatcode-isolated-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        root = self.base / "project"
        root.mkdir()
        (root / "source.txt").write_text("original", encoding="utf-8")
        (root / ".env").write_text("DUMMY_SECRET=not-copied", encoding="utf-8")
        env = patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(self.base / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.ws = Workspace(root)
        self.ctx = ToolContext(self.ws, Settings(workspace=root, tunnel="none"))
        self.addCleanup(self.shutdown)
        self.task = self.call("task_create", {"goal": "isolated", "operation_id": "create", "isolation": "copy"})
        self.identity = self.task["task_id"]
        self.copy = self.ws.tasks.execution_workspace(self.identity)

    def shutdown(self):
        if self.ws._isolated:
            self.ws._isolated.shutdown()
        if self.ws._processes:
            self.ws._processes.shutdown()

    def call(self, name, args=None, *, ctx=None):
        request = dict(args or {})
        if hasattr(self, "identity") and name not in ("task_create", "task_list"):
            request.setdefault("task_id", self.identity)
        return call_tool(name, ctx or self.ctx, request).structured

    def error(self, code, name, args=None):
        with self.assertRaises(ToolError) as caught:
            self.call(name, args)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def edit(self):
        read = self.call("read_file", {"path": "source.txt"})
        return self.call("write_file", {"path": "source.txt", "content": "changed", "expected_revision": read["revision"]})

    def finish(self):
        return self.call("task_finish", {"expected_revision": 1, "summary": "delivered"})

    def apply(self, task=None, preview=None):
        task = task or self.ws.tasks.get(self.identity)
        preview = preview or self.call("task_changes")
        return self.call("task_apply", {"expected_revision": task["revision"], "diff_revision": preview["diff_revision"], "dry_run": False})

    def run_code(self, code, check=False):
        return self.call("run_checks" if check else "exec_command", {"argv": [sys.executable, "-B", "-c", code], "yield_time_ms": 10_000})

    def test_files_commands_and_nested_batch_use_copy(self):
        self.edit()
        result = self.run_code("from pathlib import Path; Path('command.txt').write_text('from command'); print(Path.cwd())")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn(str(self.copy.root), result["stdout"])
        self.assertEqual((self.ws.root / "source.txt").read_text(), "original")
        self.assertFalse((self.ws.root / "command.txt").exists())
        self.assertFalse((self.copy.root / ".env").exists())
        batch = self.call("batch_read", {"requests": [{"tool": "read_file", "arguments": {"path": "command.txt"}}]})
        self.assertIn("from command", batch["results"][0]["result"]["content"])
        self.assertEqual(self.call("task_context")["execution_directory"], str(self.copy.root))
        self.assertEqual(self.call("task_changes")["count"], 2)

    def test_preview_apply_and_revert_restore_whole_task(self):
        self.run_code("from pathlib import Path; Path('source.txt').write_text('new'); Path('added.txt').write_text('added')")
        task = self.finish()
        preview = self.call("task_apply")
        self.assertTrue(preview["dry_run"])
        self.assertEqual((self.ws.root / "source.txt").read_text(), "original")
        applied = self.apply(task, preview)
        self.assertEqual(applied["count"], 2)
        self.assertEqual((self.ws.root / "source.txt").read_text(), "new")
        self.assertTrue((self.ws.root / "added.txt").exists())
        replay = self.apply(task, preview)
        self.assertTrue(replay["replayed"])
        before = self.call("task_revert", {"expected_revision": applied["task"]["revision"]})
        self.assertTrue(before["dry_run"])
        self.assertTrue((self.ws.root / "added.txt").exists())
        reverted = self.call("task_revert", {"expected_revision": applied["task"]["revision"], "dry_run": False})
        self.assertEqual(reverted["task"]["isolation"]["state"], "reverted")
        self.assertEqual((self.ws.root / "source.txt").read_text(), "original")
        self.assertFalse((self.ws.root / "added.txt").exists())
        self.assertTrue(self.call("task_revert", {"expected_revision": applied["task"]["revision"], "dry_run": False})["replayed"])

    def test_source_conflict_preserves_all_files(self):
        self.edit()
        self.call("write_file", {"path": "added.txt", "content": "new"})
        task = self.finish()
        (self.ws.root / "source.txt").write_text("human", encoding="utf-8")
        preview = self.call("task_changes")
        self.assertIn("source.txt", preview["conflicts"])
        self.error("ISOLATION_CONFLICT", "task_apply", {"expected_revision": task["revision"], "diff_revision": preview["diff_revision"], "dry_run": False})
        self.assertEqual((self.ws.root / "source.txt").read_text(), "human")
        self.assertFalse((self.ws.root / "added.txt").exists())

    def test_stale_preview_and_post_delivery_edit_are_refused(self):
        self.edit()
        task = self.finish()
        preview = self.call("task_changes")
        (self.ws.root / "source.txt").write_text("human", encoding="utf-8")
        self.error("REVISION_MISMATCH", "task_apply", {"expected_revision": task["revision"], "diff_revision": preview["diff_revision"], "dry_run": False})
        (self.copy.root / "new.txt").write_text("external change", encoding="utf-8")
        self.error("TASK_COPY_CHANGED", "task_apply", {"expected_revision": task["revision"], "diff_revision": self.call("task_changes")["diff_revision"], "dry_run": False})

    def test_revert_preserves_later_human_edit(self):
        self.edit()
        self.finish()
        applied = self.apply()
        (self.ws.root / "source.txt").write_text("after apply", encoding="utf-8")
        self.error("REVISION_MISMATCH", "task_revert", {"expected_revision": applied["task"]["revision"], "dry_run": False})
        self.assertEqual((self.ws.root / "source.txt").read_text(), "after apply")
        self.assertEqual(self.ws.tasks.get(self.identity)["isolation"]["state"], "applied")

    def test_restart_routes_to_same_copy_and_does_not_fall_back(self):
        self.edit()
        restored = Workspace(self.ws.root)
        self.addCleanup(lambda: restored._isolated.shutdown() if restored._isolated else None)
        ctx = replace(self.ctx, workspace=restored)
        self.assertIn("changed", self.call("read_file", {"path": "source.txt"}, ctx=ctx)["content"])
        self.assertEqual(restored.tasks.execution_workspace(self.identity).root, self.copy.root)
        (self.copy.root / "source.txt").unlink()
        self.copy.root.rmdir()
        another = Workspace(self.ws.root)
        with self.assertRaises(ToolError) as caught:
            self.call("read_file", {"path": "source.txt"}, ctx=replace(ctx, workspace=another))
        self.assertEqual(caught.exception.code, "ISOLATION_MISSING")
        self.assertEqual((self.ws.root / "source.txt").read_text(), "original")

    def test_check_fingerprint_covers_unobserved_external_copy_edits(self):
        with self.ws.tasks._db() as db:
            task = self.ws.tasks._load(db, self.identity)
            task["required_checks"] = ["tests"]
            self.ws.tasks._save(db, task)
        self.run_code("print('pass')", check=True)
        (self.copy.root / "unobserved.txt").write_text("external", encoding="utf-8")
        self.error("TASK_CHECKS_STALE", "task_finish", {"expected_revision": 1, "summary": "done"})
        self.run_code("print('pass')", check=True)
        self.assertEqual(self.finish()["status"], "completed")

    def test_creation_retry_keeps_same_copy_and_pagination(self):
        replay = self.call("task_create", {"goal": "isolated", "operation_id": "create", "isolation": "copy"})
        # 创建接口不接受已有任务的关联参数作为新请求的一部分。
        self.assertEqual(replay["task_id"], self.identity)
        for name in ("a.txt", "b.txt", "c.txt"):
            self.call("write_file", {"path": name, "content": name})
        first = self.call("task_changes", {"limit": 2})
        second = self.call("task_changes", {"offset": first["next_offset"], "limit": 2})
        self.assertEqual(first["count"], 3)
        self.assertEqual(len(first["changes"]) + len(second["changes"]), 3)

    def test_readonly_cannot_apply_or_revert_and_cancel_keeps_original(self):
        readonly = replace(self.ctx, settings=replace(self.ctx.settings, read_only=True))
        with self.assertRaises(ToolError) as caught:
            self.call("task_apply", ctx=readonly)
        self.assertEqual(caught.exception.code, "READ_ONLY")
        self.edit()
        self.call("task_cancel", {"expected_revision": 1})
        self.assertEqual((self.ws.root / "source.txt").read_text(), "original")
        self.assertEqual((self.copy.root / "source.txt").read_text(), "changed")

    def test_size_limit_refuses_partial_task_and_excluded_files_stay_excluded(self):
        with patch.object(IsolatedTasks, "MAX_BYTES", 1):
            self.error("ISOLATION_LIMIT", "task_create", {"goal": "too big", "operation_id": "large", "isolation": "copy"})
        self.assertEqual(self.ws.tasks.listing()["count"], 1)
        self.assertEqual(len(list(self.ws.isolated.store.iterdir())), 1)
        self.error("ACCESS_DENIED", "write_file", {"path": ".env", "content": "not allowed"})

    def test_apply_requires_completion_and_stops_on_source_process(self):
        self.edit()
        self.error("TASK_NOT_READY", "task_apply", {"expected_revision": 1, "diff_revision": self.call("task_changes")["diff_revision"], "dry_run": False})
        task = self.finish()
        process = call_tool("exec_command", self.ctx, {"argv": [sys.executable, "-B", "-c", "import time; time.sleep(30)"], "yield_time_ms": 0}).structured
        self.error("SOURCE_BUSY", "task_apply", {"expected_revision": task["revision"], "diff_revision": self.call("task_changes")["diff_revision"], "dry_run": False})
        self.ws.processes.stop(process["process_id"])

    def test_apply_metadata_failure_retries_without_duplicate_file_writes(self):
        self.edit()
        task = self.finish()
        preview = self.call("task_changes")
        with patch.object(self.ws.tasks, "_transition", side_effect=FileError("TASK_STORAGE_ERROR", "fault injection")):
            self.error("TASK_STORAGE_ERROR", "task_apply", {"expected_revision": task["revision"], "diff_revision": preview["diff_revision"], "dry_run": False})
        before = self.ws.editor.history()
        applied = self.apply(task, preview)
        self.assertEqual(len(self.ws.editor.history()), len(before))
        self.assertEqual(applied["task"]["isolation"]["state"], "applied")

    def test_binary_creation_and_deletion_can_be_inspected_and_reverted(self):
        (self.copy.root / "image.bin").write_bytes(b"\x00\xff\x01")
        (self.copy.root / "source.txt").unlink()
        self.finish()
        applied = self.apply()
        self.assertEqual((self.ws.root / "image.bin").read_bytes(), b"\x00\xff\x01")
        self.assertFalse((self.ws.root / "source.txt").exists())
        self.assertIn("SHA-256", self.ws.editor.details(applied["change_id"])["diff"])
        self.call("task_revert", {"expected_revision": applied["task"]["revision"], "dry_run": False})
        self.assertFalse((self.ws.root / "image.bin").exists())
        self.assertEqual((self.ws.root / "source.txt").read_text(), "original")

    def test_process_limit_is_shared_by_source_and_task_copies(self):
        other = self.call("task_create", {"goal": "other", "operation_id": "other-copy", "isolation": "copy"})
        for _ in range(4):
            self.call("exec_command", {"argv": [sys.executable, "-B", "-c", "import time; time.sleep(30)"], "yield_time_ms": 0})
        self.error("PROCESS_LIMIT", "exec_command", {"task_id": other["task_id"], "argv": [sys.executable, "-c", "print(1)"]})
        with self.assertRaises(ToolError) as caught:
            call_tool("exec_command", self.ctx, {"argv": [sys.executable, "-c", "print(1)"]})
        self.assertEqual(caught.exception.code, "PROCESS_LIMIT")
