"""真实 HTTP 验证项目热切换、旧请求绑定与项目资源隔离。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.request
from unittest.mock import patch

from chatcode.config import Settings, build_arg_parser, settings_from_args
from chatcode.runtime import ServiceRuntime
from chatcode.tools import call_tool, ToolContext, ToolError
from chatcode.workspace import Workspace, PathRejected


class ProjectCase(unittest.TestCase):
    def test_isolated_task_keeps_origin_binding_after_hot_switch(self):
        task = self.call("task_create", {"goal": "HTTP copy", "operation_id": "http-copy", "isolation": "copy"}, self.context_a)["structuredContent"]
        self.runtime.switch_workspace(self.b)
        self.call("write_file", {"task_id": task["task_id"], "path": "new.txt", "content": "copy"}, self.context_a)
        self.assertFalse((self.a / "new.txt").exists())
        self.assertFalse((self.b / "new.txt").exists())
        done = self.call("task_finish", {"task_id": task["task_id"], "expected_revision": 1, "summary": "done"}, self.context_a)["structuredContent"]
        preview = self.call("task_changes", {"task_id": task["task_id"]}, self.context_a)["structuredContent"]
        applied = self.call("task_apply", {"task_id": task["task_id"], "expected_revision": done["revision"], "diff_revision": preview["diff_revision"], "dry_run": False}, self.context_a)["structuredContent"]
        self.assertTrue((self.a / "new.txt").exists())
        self.assertFalse((self.b / "new.txt").exists())
        self.call("task_revert", {"task_id": task["task_id"], "expected_revision": applied["task"]["revision"], "dry_run": False}, self.context_a)
        self.assertFalse((self.a / "new.txt").exists())

    def test_task_http_flow_survives_project_switch(self):
        task = self.call("task_create", {"goal": "HTTP task", "operation_id": "http-task"}, self.context_a)["structuredContent"]
        self.runtime.switch_workspace(self.b)
        context_b = self.call("server_info")["structuredContent"]
        args = {"task_id": task["task_id"], "path": "task.txt", "content": "bound to A"}
        rejected = self.call("write_file", args, context_b)
        self.assertEqual(rejected["structuredContent"]["code"], "TASK_NOT_FOUND")
        changed = self.call("write_file", args, self.context_a)
        self.assertFalse(changed.get("isError"))
        self.assertTrue((self.a / "task.txt").exists())
        self.assertFalse((self.b / "task.txt").exists())
        context = self.call("task_context", {"task_id": task["task_id"]}, self.context_a)["structuredContent"]
        self.assertEqual(context["files"][0]["state"], "unchanged")
        done = self.call("task_finish", {"task_id": task["task_id"], "expected_revision": 1, "summary": "HTTP verified"}, self.context_a)
        self.assertEqual(done["structuredContent"]["status"], "completed")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatcode-projects-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.a, self.b = self.root / "A", self.root / "B"
        for path in (self.a, self.b):
            path.mkdir()
            (path / "same.txt").write_text("original", encoding="utf-8")
        environment = patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(self.root / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        tunnel = SimpleNamespace(public_url="https://project-fixture.invalid", process=None, stop=lambda: None)
        start = patch("chatcode.runtime.start_tunnel", return_value=tunnel)
        self.start_tunnel = start.start()
        self.addCleanup(start.stop)
        self.runtime = ServiceRuntime(Settings(workspace=self.a, port=0, tunnel="none"), log=lambda _: None)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.assertTrue(self.runtime.ready.wait(3))
        self.url = f"http://127.0.0.1:{self.runtime.settings.port}" + self.runtime.settings.mcp_path()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.context_a = self.call("server_info")["structuredContent"]

    def call(self, name, args=None, context=None):
        arguments = dict(args or {})
        if context:
            arguments.update({key: context[key] for key in ("workspace_id", "workspace_token")})
        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": name, "arguments": arguments}}).encode()
        request = urllib.request.Request(self.url, data=raw, headers={"Content-Type": "application/json"})
        with self.opener.open(request, timeout=15) as response:
            payload = json.load(response)
        self.assertNotIn("error", payload)
        return payload["result"]

    def test_switch_keeps_connection_and_old_context_routes_to_original(self):
        server, tunnel, thread = self.runtime.server, self.runtime.tunnel, self.runtime._http_thread
        public = self.runtime.settings.mcp_url()
        revision = self.call("read_file", {"path": "same.txt"}, self.context_a)["structuredContent"]["revision"]
        self.runtime.switch_workspace(self.b)
        context_b = self.call("server_info")["structuredContent"]
        self.assertEqual(context_b["workspace"], str(self.b.resolve()))
        self.assertEqual(self.runtime.settings.mcp_url(), public)
        self.assertIs(self.runtime.server, server)
        self.assertIs(self.runtime.tunnel, tunnel)
        self.assertIs(self.runtime._http_thread, thread)
        self.assertEqual(self.start_tunnel.call_count, 1)
        rejected = self.call("write_file", {"path": "same.txt", "content": "wrong", "expected_revision": revision})
        self.assertEqual(rejected["structuredContent"]["code"], "WORKSPACE_REQUIRED")
        changed = self.call("write_file", {"path": "same.txt", "content": "A changed", "expected_revision": revision}, self.context_a)
        self.assertFalse(changed["isError"])
        self.assertEqual((self.a / "same.txt").read_text(), "A changed")
        self.assertEqual((self.b / "same.txt").read_text(), "original")
        wrong = dict(self.context_a, workspace_token=context_b["workspace_token"])
        self.assertEqual(self.call("read_file", {"path": "same.txt"}, wrong)["structuredContent"]["code"], "WORKSPACE_CONTEXT_INVALID")
        self.runtime.switch_workspace(self.a)
        self.assertEqual(self.call("read_file", {"path": "same.txt"})["structuredContent"]["code"], "WORKSPACE_REQUIRED")

    def test_old_process_and_undo_remain_in_original_project(self):
        change = self.call("write_file", {"path": "created.txt", "content": "A only"}, self.context_a)["structuredContent"]
        process = self.call("exec_command", {"argv": [sys.executable, "-B", "-c",
            "import os,time;print(os.getcwd(),flush=True);time.sleep(0.5);print('DONE')"], "yield_time_ms": 0}, self.context_a)["structuredContent"]
        self.runtime.switch_workspace(self.b)
        context_b = self.call("server_info")["structuredContent"]
        self.assertEqual(self.call("server_info", context=context_b)["structuredContent"]["processes"], [])
        wrong = self.call("read_process", {"process_id": process["process_id"]}, context_b)
        self.assertEqual(wrong["structuredContent"]["code"], "PROCESS_NOT_FOUND")
        result = self.call("read_process", {"process_id": process["process_id"], "wait_ms": 2000}, self.context_a)
        self.assertIn(str(self.a), result["structuredContent"]["stdout"])
        self.assertIn("DONE", result["structuredContent"]["stdout"])
        self.assertEqual(self.call("get_changes", context=context_b)["structuredContent"]["count"], 0)
        undo = self.call("undo_changes", {"change_id": change["change_id"]}, self.context_a)
        self.assertFalse(undo["isError"])
        self.assertFalse((self.a / "created.txt").exists())
        self.assertFalse((self.b / "created.txt").exists())

    def test_inflight_request_keeps_bound_workspace_during_switch(self):
        entered, release = threading.Event(), threading.Event()
        revision = self.call("read_file", {"path": "same.txt"})["structuredContent"]["revision"]
        original = call_tool
        def delayed(name, ctx, args):
            if name == "write_file":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test barrier timed out")
            return original(name, ctx, args)
        with patch("chatcode.mcp_http.call_tool", side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.call, "write_file", {"path": "same.txt", "content": "inflight A", "expected_revision": revision})
            try:
                self.assertTrue(entered.wait(3))
                self.runtime.switch_workspace(self.b)
            finally:
                release.set()
            self.assertFalse(future.result(timeout=5)["isError"])
        self.assertEqual((self.a / "same.txt").read_text(), "inflight A")
        self.assertEqual((self.b / "same.txt").read_text(), "original")

    def test_registered_only_and_batch_cannot_change_project(self):
        self.runtime.switch_workspace(self.b)
        context_b = self.call("server_info")["structuredContent"]
        listed = self.call("list_workspaces")["structuredContent"]
        self.assertEqual(len(listed["workspaces"]), 2)
        self.assertTrue(listed["context_required"])
        nested = {"path": "same.txt", "workspace_id": context_b["workspace_id"], "workspace_token": context_b["workspace_token"]}
        batch = self.call("batch_read", {"requests": [{"tool": "read_file", "arguments": nested}]}, self.context_a)
        self.assertEqual(batch["structuredContent"]["results"][0]["code"], "WORKSPACE_CONTEXT_INVALID")
        forged = dict(self.context_a, workspace_id="0" * 24)
        self.assertEqual(self.call("read_file", {"path": "same.txt"}, forged)["structuredContent"]["code"], "WORKSPACE_CONTEXT_INVALID")

    def test_profiles_rules_and_invalid_switch(self):
        (self.b / ".chatcodeignore").write_text("same.txt", encoding="utf-8")
        project = self.runtime.switch_workspace(self.b, settings=replace(self.runtime.settings,
            language_servers={"go": ["fixture-language-server"]}, read_only=True))
        self.assertEqual(project.settings.language_servers, {"go": ["fixture-language-server"]})
        info = self.call("server_info")["structuredContent"]
        self.assertTrue(info["read_only"])
        self.assertEqual(self.call("read_file", {"path": "same.txt"}, info)["structuredContent"]["code"], "ACCESS_DENIED")
        self.assertEqual(self.call("write_file", {"path": "new.txt", "content": "x"}, info)["structuredContent"]["code"], "READ_ONLY")
        self.assertIn("original", self.call("read_file", {"path": "same.txt"}, self.context_a)["content"][0]["text"])
        with self.assertRaises(ValueError):
            self.runtime.switch_workspace(self.root / "missing")
        self.assertIs(self.runtime.server.projects.active, project)

    def test_shutdown_closes_every_project_and_blocks_lazy_managers(self):
        original = self.runtime.server.workspace
        self.runtime.switch_workspace(self.b)
        selected = self.runtime.server.workspace
        self.runtime.stop()
        for workspace in (original, selected):
            with self.assertRaises(PathRejected):
                _ = workspace.processes

    def test_cli_can_register_additional_projects(self):
        args = build_arg_parser().parse_args(["--workspace", str(self.a), "--project", str(self.b)])
        self.assertEqual(settings_from_args(args).projects, [str(self.b)])


class SearchAndGitCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatcode-tools-07-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        environment = patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(self.root / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.workspace = Workspace(self.root)
        self.ctx = ToolContext(self.workspace, Settings(workspace=self.root, tunnel="none"))
        self.addCleanup(lambda: self.workspace._processes.shutdown() if self.workspace._processes else None)

    def test_catastrophic_regex_is_terminated_and_service_can_search_again(self):
        (self.root / "fixture.txt").write_text("a" * 30 + "!", encoding="utf-8")
        started = time.monotonic()
        with self.assertRaises(ToolError) as error:
            call_tool("search_text", self.ctx, {"pattern": "(a+)+$", "regex": True, "timeout_ms": 1000})
        self.assertEqual(error.exception.code, "SEARCH_TIMEOUT")
        self.assertLess(time.monotonic() - started, 4)
        found = call_tool("search_text", self.ctx, {"pattern": "a+", "regex": True})
        self.assertEqual(found.structured["count"], 1)

    def test_regex_preserves_loaded_access_policy_and_reports_bad_patterns(self):
        (self.root / "private.txt").write_text("MATCH", encoding="utf-8")
        (self.root / ".chatcodeignore").write_text("private.txt", encoding="utf-8")
        self.ctx.workspace = Workspace(self.root)
        (self.root / ".chatcodeignore").write_text("", encoding="utf-8")
        result = call_tool("search_text", self.ctx, {"pattern": "MATCH", "regex": True})
        self.assertEqual(result.structured["count"], 0)
        with self.assertRaises(ToolError) as error:
            call_tool("search_text", self.ctx, {"pattern": "[", "regex": True})
        self.assertEqual(error.exception.code, "BAD_REGEX")

    def test_javascript_test_alias_runs_configured_script(self):
        (self.root / ".chatcode.toml").write_text('[checks]\ntest = ' + json.dumps([sys.executable, "-c", "print('OK')"]), encoding="utf-8")
        # 显式配置的 test 同样接受默认 tests 别名。
        result = call_tool("run_checks", self.ctx, {"yield_time_ms": 10_000})
        self.assertIn("OK", result.structured["stdout"])
        (self.root / ".chatcode.toml").unlink()
        (self.root / "package.json").write_text(json.dumps({"scripts": {"test": "echo OK"}}), encoding="utf-8")
        self.ctx.settings.allow_exec = False
        with self.assertRaises(ToolError) as error:
            call_tool("run_checks", self.ctx, {})
        # 必须成功识别 npm 的 test，再由执行权限阻止；不能报 CHECK_NOT_CONFIGURED。
        self.assertEqual(error.exception.code, "EXEC_DISABLED")

    def test_git_status_roundtrips_unicode_spaces_and_rename(self):
        if not shutil.which("git"):
            self.skipTest("需要 Git")
        def git(*args):
            return subprocess.run(["git", *args], cwd=self.root, capture_output=True, check=True)
        git("init", "-q")
        git("config", "user.name", "fixture")
        git("config", "user.email", "fixture@example.invalid")
        name = "中文 文件.py"
        (self.root / name).write_text("fixture", encoding="utf-8")
        entries = call_tool("git_status", self.ctx, {}).structured["entries"]
        self.assertIn(name, [item["path"] for item in entries])
        git("add", name)
        git("commit", "-qm", "fixture")
        git("mv", name, "新的 文件.py")
        item = next(item for item in call_tool("git_status", self.ctx, {}).structured["entries"] if "R" in item["status"])
        self.assertEqual(item["path"], "新的 文件.py")
        self.assertEqual(item["original_path"], name)
        nested = self.root / "nested"
        nested.mkdir()
        (nested / "inside.txt").write_text("old", encoding="utf-8")
        git("add", ".")
        git("commit", "-qm", "nested fixture")
        (nested / "inside.txt").write_text("inside", encoding="utf-8")
        (self.root / "新的 文件.py").write_text("outside", encoding="utf-8")
        self.ctx.workspace = Workspace(nested)
        entries = call_tool("git_status", self.ctx, {}).structured["entries"]
        self.assertEqual([entry["path"] for entry in entries], ["inside.txt"])


if __name__ == "__main__":
    unittest.main()
