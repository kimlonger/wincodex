"""任务恢复、执行证据、交付门禁和项目隔离。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from chatcode.config import Settings
from chatcode.tools import ToolContext, ToolError, call_tool
from chatcode.workspace import Workspace


class TaskCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="chatcode-tasks-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        env = patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.ws = Workspace(self.project)
        self.ctx = ToolContext(self.ws, Settings(workspace=self.project, tunnel="none"))
        self.addCleanup(lambda: self.ws._processes.shutdown() if self.ws._processes is not None else None)
        (self.project / "README.md").write_text("# 项目说明", encoding="utf-8")
        (self.project / "AGENTS.md").write_text("修改后检查 tests", encoding="utf-8")
        self.number = 0

    def call(self, name, args=None, task=None, ctx=None):
        request = dict(args or {})
        if task:
            request["task_id"] = task["task_id"]
        return call_tool(name, ctx or self.ctx, request).structured

    def create(self, **args):
        self.number += 1
        return self.call("task_create", {"goal": "完成示例功能", "operation_id": f"task-{self.number}", **args})

    def error(self, code, name, args=None, task=None, ctx=None):
        with self.assertRaises(ToolError) as caught:
            self.call(name, args, task, ctx)
        self.assertEqual(caught.exception.code, code, str(caught.exception))

    def finish(self, task, **args):
        return self.call("task_finish", {"expected_revision": task["revision"], "summary": "已完成", **args}, task)

    def run_code(self, task, code="print('verified')", *, check=False, **args):
        return self.call("run_checks" if check else "exec_command", {
            "argv": [sys.executable, "-B", "-c", code], "yield_time_ms": 10_000, **args}, task)

    def test_creation_retries_and_progress_conflict(self):
        task = self.create(operation_id="stable")
        repeated = self.create(operation_id="stable")
        self.assertEqual(task["task_id"], repeated["task_id"])
        self.assertTrue(repeated["replayed"])
        self.error("OPERATION_ID_REUSED", "task_create", {"goal": "不同目标", "operation_id": "stable"})
        def change(summary):
            try:
                return self.call("task_update", {"expected_revision": 1, "summary": summary}, task)
            except ToolError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(change, ["A", "B"]))
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1)
        self.assertIn("TASK_REVISION_MISMATCH", results)

    def test_association_checks_delivery_and_idempotent_finish(self):
        task = self.create(acceptance_criteria=["新增文件"], required_checks=["tests"])
        changed = self.call("edit_files", {"files": [{"action": "create", "path": "example.txt", "content": "content"}], "operation_id": "edit-1"}, task)
        self.assertEqual(self.ws.editor.history()[0]["task_id"], task["task_id"])
        self.error("TASK_EVIDENCE_REQUIRED", "task_finish", {"expected_revision": 1, "summary": "done"}, task)
        self.error("TASK_CHECKS_FAILED", "task_finish", {"expected_revision": 1, "summary": "done", "criteria_evidence": ["example.txt"]}, task)
        check = self.run_code(task, check=True, name="test", operation_id="check-1")
        self.assertEqual(check["exit_code"], 0)
        context = self.call("task_context", task=task)
        self.assertEqual(context["files"][0]["state"], "unchanged")
        self.assertEqual(context["processes"][0]["task_id"], task["task_id"])
        self.assertIn("verified", context["processes"][0]["stdout_tail"])
        self.assertEqual(context["changes"][0]["change_id"], changed["change_id"])
        self.assertEqual({item["path"] for item in context["project_materials"]}, {"README.md", "AGENTS.md"})
        done = self.finish(task, criteria_evidence=["example.txt 已新增"])
        self.assertEqual(done["status"], "completed")
        self.assertTrue(self.finish(task, criteria_evidence=["example.txt 已新增"])["replayed"])
        self.error("TASK_NOT_RUNNING", "write_file", {"path": "later.txt", "content": "x"}, task)

    def test_stale_failed_and_zero_test_checks_cannot_deliver(self):
        task = self.create(required_checks=["tests"])
        self.run_code(task, check=True)
        self.call("write_file", {"path": "code.txt", "content": "new"}, task)
        self.error("TASK_CHECKS_STALE", "task_finish", {"expected_revision": 1, "summary": "done"}, task)
        self.run_code(task, "print('Ran 0 tests')", check=True)
        self.error("TASK_CHECKS_FAILED", "task_finish", {"expected_revision": 1, "summary": "done"}, task)
        self.run_code(task, "import sys; sys.exit(2)", check=True)
        self.error("TASK_CHECKS_FAILED", "task_finish", {"expected_revision": 1, "summary": "done"}, task)
        self.run_code(task, check=True)
        self.assertEqual(self.finish(task)["status"], "completed")

    def test_rereading_external_change_invalidates_checks_and_aliases_use_latest(self):
        task = self.create(required_checks=["tests"])
        self.call("write_file", {"path": "tracked.txt", "content": "before"}, task)
        self.run_code(task, check=True, name="tests")
        (self.project / "tracked.txt").write_text("after", encoding="utf-8")
        self.call("read_file", {"path": "tracked.txt"}, task)
        self.error("TASK_CHECKS_STALE", "task_finish", {"expected_revision": 1, "summary": "done"}, task)
        self.run_code(task, "import sys; sys.exit(1)", check=True, name="test")
        self.error("TASK_CHECKS_FAILED", "task_finish", {"expected_revision": 1, "summary": "done"}, task)
        self.run_code(task, check=True, name="test")
        self.assertEqual(self.finish(task)["status"], "completed")

    def test_missing_execution_result_cannot_be_marked_complete(self):
        task = self.create()
        identity = self.ws.tasks.begin(task["task_id"], "edit_files", mutating=True)
        context = self.call("task_context", task=task)
        self.assertEqual(context["unfinished_calls"][0]["call_id"], identity)
        self.error("TASK_EXECUTION_UNCERTAIN", "task_finish", {"expected_revision": 1, "summary": "done"}, task)

    def test_external_edits_require_reread_and_dry_run_is_not_observation(self):
        task = self.create()
        self.call("write_file", {"path": "code.txt", "content": "first"}, task)
        (self.project / "code.txt").write_text("human edit", encoding="utf-8")
        self.error("TASK_FILES_CHANGED", "task_finish", {"expected_revision": 1, "summary": "done"}, task)
        read = self.call("read_file", {"path": "code.txt"}, task)
        self.call("edit_files", {"files": [{"path": "code.txt", "action": "delete", "expected_revision": read["revision"]}], "dry_run": True}, task)
        self.assertEqual(self.call("task_context", task=task)["files"][0]["state"], "unchanged")
        self.assertEqual(self.finish(task)["status"], "completed")

    def test_pause_cancel_process_ownership_and_busy_delivery(self):
        task, other = self.create(), self.create()
        self.call("write_file", {"path": "kept.txt", "content": "kept"}, task)
        process = self.run_code(task, "import time; time.sleep(30)", yield_time_ms=0)
        self.error("TASK_BUSY", "task_finish", {"expected_revision": 1, "summary": "done"}, task)
        self.error("TASK_RESOURCE_MISMATCH", "stop_process", {"process_id": process["process_id"]}, other)
        paused = self.call("task_update", {"expected_revision": 1, "status": "paused"}, task)
        self.error("TASK_NOT_RUNNING", "write_file", {"path": "blocked.txt", "content": "x"}, task)
        self.assertIn("kept", self.call("read_file", {"path": "kept.txt"}, task)["content"])
        self.assertTrue(self.call("task_context", task=task)["processes"][0]["running"])
        cancelled = self.call("task_cancel", {"expected_revision": paused["revision"]}, task)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(self.call("task_context", task=task)["processes"][0]["running"])
        self.assertTrue((self.project / "kept.txt").exists())

    def test_restart_restores_context_without_replaying_commands(self):
        task = self.create()
        self.run_code(task, "from pathlib import Path; Path('once.txt').write_text('once'); print('saved log')", operation_id="once")
        task = self.call("task_update", {"expected_revision": 1, "summary": "已执行命令", "decisions": ["保留实现"], "next_steps": ["检查交付"]}, task)
        self.ws.processes.shutdown()
        restored_ws = Workspace(self.project)
        restored = replace(self.ctx, workspace=restored_ws)
        context = self.call("task_context", task=task, ctx=restored)
        self.assertTrue(context["task"]["recovery_required"])
        self.assertEqual(context["task"]["next_steps"], ["检查交付"])
        self.assertIn("saved log", context["processes"][0]["stdout_tail"])
        self.error("TASK_NOT_RUNNING", "exec_command", {"argv": [sys.executable, "-c", "raise Exception()"]}, task, restored)
        resumed = self.call("task_update", {"expected_revision": task["revision"], "status": "running"}, task, restored)
        self.assertEqual(resumed["owner_run"], restored_ws.run_id)
        self.assertEqual((self.project / "once.txt").read_text(), "once")
        restored_ws.tasks.interrupt()
        self.assertEqual(restored_ws.tasks.get(task["task_id"])["status"], "interrupted")

    def test_unknown_old_process_does_not_become_success(self):
        task = self.create()
        self.ws.tasks.process_update({"task_id": task["task_id"], "process_id": "f" * 32, "state": "running", "started_at": 1})
        self.error("TASK_BUSY", "task_finish", {"expected_revision": 1, "summary": "done"}, task)
        context = self.call("task_context", task=task)
        self.assertEqual(context["processes"][0]["state"], "unknown_after_restart")

    def test_other_project_and_other_task_cannot_claim_resources(self):
        task, other = self.create(), self.create()
        request = {"files": [{"action": "create", "path": "a.txt", "content": "a"}], "operation_id": "owned"}
        changed = self.call("edit_files", request, task)
        self.error("TASK_RESOURCE_MISMATCH", "edit_files", request, other)
        self.error("TASK_RESOURCE_MISMATCH", "undo_changes", {"change_id": changed["change_id"]}, other)
        directory = self.root / "other"
        directory.mkdir()
        self.error("TASK_NOT_FOUND", "read_file", {"path": "a.txt"}, task, replace(self.ctx, workspace=Workspace(directory)))
        bound = replace(self.ctx, task_id=task["task_id"])
        self.error("TASK_CONTEXT_INVALID", "read_file", {"path": "a.txt"}, other, bound)

    def test_readonly_and_legacy_tools(self):
        task = self.create()
        readonly = replace(self.ctx, settings=replace(self.ctx.settings, read_only=True))
        self.error("READ_ONLY", "task_create", {"goal": "x", "operation_id": "ro"}, ctx=readonly)
        self.error("READ_ONLY", "task_update", {"expected_revision": 1, "status": "paused"}, task, readonly)
        self.assertEqual(self.call("task_context", task=task, ctx=readonly)["task"]["goal"], task["goal"])
        self.call("write_file", {"path": "legacy.txt", "content": "legacy"})
        self.assertIsNone(self.ws.editor.history()[0]["task_id"])
        self.assertNotIn("task_id", self.call("read_file", {"path": "legacy.txt"}))

    def test_event_pagination_and_response_budget(self):
        task = self.create()
        for _ in range(3):
            self.call("read_file", {"path": "README.md"}, task)
        first = self.call("task_get", {"after_seq": 0, "event_limit": 2}, task)
        second = self.call("task_get", {"after_seq": first["next_after_seq"], "event_limit": 2}, task)
        self.assertTrue(first["has_more"])
        self.assertLess(first["events"][-1]["seq"], second["events"][0]["seq"])
        self.assertLess(len(json.dumps(self.call("task_context", task=task)).encode()), 120_000)
