"""索引增量、访问策略、上下文预算、归档回收与原生沙箱的失败关闭。"""

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatcode.config import Settings, build_arg_parser, settings_from_args
from chatcode.files import FileError
from chatcode.tools import ToolContext, ToolError, call_tool
from chatcode.workspace import Workspace


class GrowthCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="chatcode-growth-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        env = patch.dict(os.environ, CHATCODE_DATA_DIR=str(self.base / "state"))
        env.start()
        self.addCleanup(env.stop)
        self.ws = Workspace(self.root)
        self.ctx = ToolContext(self.ws, Settings(workspace=self.root))
        self.addCleanup(lambda: self.ws.isolated.shutdown() if self.ws._isolated else None)

    def put(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def call(self, name, **args):
        return call_tool(name, self.ctx, args).structured

    def error(self, code, name, **args):
        with self.assertRaises(ToolError) as caught:
            self.call(name, **args)
        self.assertEqual(caught.exception.code, code)

    def create(self, **args):
        return self.call("task_create", goal="test", operation_id="create", isolation="copy", **args)

    def cancel_archive(self, task):
        task = self.call("task_cancel", task_id=task["task_id"], expected_revision=task["revision"])
        return self.call("task_archive", task_id=task["task_id"], expected_revision=task["revision"])["task"]

    def test_java_go_navigation_candidates_ignore_comments(self):
        self.put("a.go", '// func Fake() {}\npackage sample\nfunc Hello() string { return "}" }\n')
        self.put("A.java", '/* public void fake() {} */\npublic class A {\n public int count() { return 1; }\n}\n')
        for path, symbol in (("a.go", "Hello"), ("A.java", "count")):
            data = self.call("code_outline", path=path)
            self.assertEqual(data["backend"], "lexical_candidates")
            self.assertTrue(any(item["name"] == symbol for item in data["symbols"]))
            self.assertFalse(any(item["name"] in ("Fake", "fake") for item in data["symbols"]))
            code = self.call("read_code", path=path, symbol=symbol)
            self.assertIn(symbol, code["content"])
            self.assertFalse(code["symbol"]["semantic"])

    def test_incremental_index_restart_update_delete_and_resume(self):
        for i in range(7):
            self.put(f"a{i}.py", f"def indexed{i}(): return {i}\n")
        first = self.call("refresh_index", max_files=2)
        self.assertEqual(first["updated"], 2)
        self.assertFalse(first["complete"])
        self.assertEqual(self.call("refresh_index", max_files=2)["updated"], 2)
        self.call("refresh_index", max_files=10)
        self.assertEqual(self.call("refresh_index")["updated"], 0)
        self.ws._index = None
        self.assertEqual(self.call("index_status")["files"], 7)
        self.put("a2.py", "def revised(): return 22\n")
        (self.root / "a3.py").unlink()
        self.assertEqual(self.call("find_symbol", query="revised")["count"], 1)
        data = self.call("find_symbol", query="indexed")
        self.assertEqual(data["count"], 5)
        self.assertEqual(self.call("index_status")["files"], 6)

    def test_index_does_not_return_stale_or_newly_denied_material(self):
        self.put("hidden.py", "def private_name(): pass\n")
        self.call("refresh_index")
        self.put("hidden.py", "def public_name(): pass\n")
        self.assertEqual(self.ws.index.search("private_name")["count"], 0)
        self.put(".chatcodeignore", "hidden.py\n")
        other = Workspace(self.root)
        self.assertEqual(other.index.search("private_name")["count"], 0)
        self.assertEqual(other.index.search("public_name")["count"], 0)
        other.index.refresh()
        self.assertEqual(other.index.status()["files"], 0)

    def test_context_budget_pagination_and_no_source_bodies_in_index(self):
        for i in range(12):
            self.put(f"a{i}.py", f"def common{i}():\n    return 'BODY_MARKER" + "文" * 1000 + "'\n")
        pack = self.call("context_pack", query="common", budget_chars=1500)
        self.assertLessEqual(pack["used_chars"], 1500)
        self.assertTrue(pack["truncated"])
        self.assertTrue(pack["files"])
        self.assertNotIn(b"BODY_MARKER", self.ws.index.path.read_bytes())
        first = self.call("find_symbol", query="common", limit=2)
        second = self.call("find_symbol", query="common", limit=2, offset=first["next_offset"])
        self.assertFalse({s['path'] for s in first['symbols']} & {s['path'] for s in second['symbols']})

    def test_cached_parse_error_and_oversized_symbol_do_not_fake_complete_results(self):
        self.put("invalid.py", "def broken(\n")
        self.call("refresh_index")
        data = self.call("find_symbol", query="broken")
        self.assertEqual(data["skipped"][0]["code"], "SYNTAX_ERROR")
        source = "".join("    " * i + "class " + "X" * 140 + str(i) + ":\n" for i in range(10))
        self.put("nested.py", source + "    " * 10 + "def unique_target(): pass\n")
        self.error("OUTPUT_BUDGET_TOO_SMALL", "find_symbol", query="unique_target", budget_chars=1000)

    def test_go_java_project_check_suggestions_and_detection_do_not_execute(self):
        self.put("go.mod", "module example.com/demo\n")
        self.assertIn("go test ./...", [item["command"] for item in self.call("project_overview")["suggested_checks"]])
        self.put("pom.xml", "<project/>")
        self.assertIn("mvn test", [item["command"] for item in self.call("project_overview")["suggested_checks"]])
        with patch("subprocess.Popen", side_effect=AssertionError("must not run")):
            self.assertFalse(self.call("language_status")["automatically_enabled"])

    def test_archive_list_export_preview_cleanup_keeps_history_and_source(self):
        self.put("source.txt", "SOURCE")
        task = self.cancel_archive(self.create())
        self.assertEqual(self.call("task_list")["count"], 0)
        self.assertEqual(self.call("task_list", archived=True)["count"], 1)
        exported = self.call("task_export", task_id=task["task_id"])
        data = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
        self.assertEqual(data["task"]["task_id"], task["task_id"])
        plan = self.call("task_cleanup", older_than_days=0)
        copy = self.ws.isolated.directory(task["task_id"])
        self.assertTrue(copy.exists())
        self.error("CLEANUP_PLAN_CHANGED", "task_cleanup", dry_run=False, older_than_days=0)
        outcome = self.call("task_cleanup", dry_run=False, older_than_days=0, plan_revision=plan["plan_revision"])
        self.assertEqual(outcome["removed_task_ids"], [task["task_id"]])
        self.assertFalse(copy.exists())
        self.assertEqual((self.root / "source.txt").read_text(), "SOURCE")
        self.assertTrue(self.call("task_get", task_id=task["task_id"])["task"]["isolation"]["released"])
        self.error("TASK_COPY_RELEASED", "read_file", task_id=task["task_id"], path="source.txt")
        self.error("TASK_ARCHIVED", "write_file", task_id=task["task_id"], path="source.txt", content="BAD")

    def test_cleanup_rejects_changed_preview_and_active_tasks(self):
        task = self.create()
        self.error("TASK_NOT_CLOSED", "task_archive", task_id=task["task_id"], expected_revision=1)
        self.assertEqual(self.call("task_cleanup", older_than_days=0)["candidates"], [])
        task = self.cancel_archive(task)
        plan = self.call("task_cleanup", older_than_days=0)
        (self.ws.isolated.directory(task["task_id"]) / "work" / "new.txt").write_text("keep")
        self.error("CLEANUP_PLAN_CHANGED", "task_cleanup", older_than_days=0, dry_run=False, plan_revision=plan["plan_revision"])

    def test_quota_stops_new_copies_without_touching_source(self):
        task = self.create()
        self.ctx.settings.storage_quota_mb = 0
        self.put("new.py", "def x(): pass\n")
        self.error("STORAGE_QUOTA", "task_create", goal="new", operation_id="new", isolation="copy")
        self.assertTrue((self.root / "new.py").exists())
        self.assertEqual(len(list(self.ws.isolated.store.iterdir())), 1)

    def test_cleanup_keeps_applied_revert_receipt(self):
        self.put("source.txt", "original")
        task = self.create()
        identity = task["task_id"]
        read = self.call("read_file", task_id=identity, path="source.txt")
        self.call("write_file", task_id=identity, path="source.txt", content="improved", expected_revision=read["revision"])
        task = self.call("task_finish", task_id=identity, expected_revision=1, summary="done")
        preview = self.call("task_changes", task_id=identity)
        task = self.call("task_apply", task_id=identity, expected_revision=task["revision"], diff_revision=preview["diff_revision"], dry_run=False)["task"]
        task = self.call("task_archive", task_id=identity, expected_revision=task["revision"])["task"]
        plan = self.call("task_cleanup", older_than_days=0)
        self.call("task_cleanup", older_than_days=0, dry_run=False, plan_revision=plan["plan_revision"])
        self.assertTrue(self.call("task_context", task_id=identity)["historical_only"])
        task = self.call("task_get", task_id=identity)["task"]
        self.call("task_revert", task_id=identity, expected_revision=task["revision"], dry_run=False)
        self.assertEqual((self.root / "source.txt").read_text(), "original")

    def test_native_mode_requires_copy_and_fails_closed(self):
        self.ctx.settings.execution_mode = "native"
        self.error("SANDBOX_COPY_REQUIRED", "task_create", goal="bad", operation_id="bad", isolation="direct")
        task = self.call("task_create", goal="native", operation_id="native")
        self.assertEqual(task["isolation"]["mode"], "copy")
        with patch("chatcode.sandbox._api", side_effect=FileError("SANDBOX_UNAVAILABLE", "disabled")), patch("subprocess.Popen", side_effect=AssertionError("must not fall back")):
            self.error("SANDBOX_UNAVAILABLE", "exec_command", task_id=task["task_id"], argv=["python", "-c", "print('BAD')"])
        self.error("SANDBOX_TOOL_DISABLED", "git_status")

    def test_native_cli_profile_readonly_and_language_launch_guard(self):
        args = build_arg_parser().parse_args(["--workspace", str(self.root), "--execution-mode", "native"])
        settings = settings_from_args(args)
        self.assertEqual(settings.execution_mode, "native")
        self.assertFalse(hasattr(settings, "sandbox_image"))
        self.ctx.settings = settings
        self.put("Demo.java", "public class Demo {}")
        from chatcode.lsp import services
        self.error("SANDBOX_LSP_DISABLED", "code_outline", path="Demo.java", backend="lsp")
        self.ctx.settings = replace(settings, read_only=True)
        self.error("READ_ONLY", "task_cleanup", dry_run=False)

    def test_native_status_and_server_info_agree_on_internet_access(self):
        self.ctx.settings.execution_mode = "native"
        info = self.call("server_info")["sandbox_policy"]
        status = self.call("sandbox_status")
        self.assertEqual(info["network"], "internet")
        self.assertEqual(info["network_capabilities"], ["internetClient"])
        self.assertEqual(status["network_capabilities"], info["network_capabilities"])
        self.assertFalse(info["fallback_to_host"])


if __name__ == "__main__":
    unittest.main()
