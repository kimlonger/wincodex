"""正式版权限、Git 分页与命令重试的端到端回归。"""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from chatcode.config import Settings, build_arg_parser, settings_from_args
from chatcode.mcp_http import create_server, MCPHandler
from chatcode.tools import TOOLS, ToolContext, ToolError, call_tool, is_read_only_tool
from chatcode.workspace import Workspace


class ReleaseCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatcode-release-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        environment = patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(self.base / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        self.ws = Workspace(self.root)
        self.settings = Settings(workspace=self.root, tunnel="none", port=0)
        self.ctx = ToolContext(self.ws, self.settings)
        self.addCleanup(lambda: self.ws._processes.shutdown() if self.ws._processes else None)

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def assert_code(self, code, name, args):
        with self.assertRaises(ToolError) as result:
            call_tool(name, self.ctx, args)
        self.assertEqual(result.exception.code, code)

    def git(self, *arguments):
        return subprocess.run(["git", *arguments], cwd=self.root, capture_output=True, check=True)

    def init_git(self):
        if not shutil.which("git"):
            self.skipTest("需要本地 Git")
        self.git("init", "-q")
        self.git("config", "user.name", "ChatCode Test")
        self.git("config", "user.email", "test@example.invalid")

    def commit(self):
        self.git("add", ".")
        self.git("commit", "-qm", "fixture")

    def test_sensitive_files_denied_across_direct_batch_search_and_edit(self):
        for name in (".env", "nested/.env.production", "private.key", "keys/id_ed25519"):
            self.write(name, "PRIVATE_FIXTURE_MARKER")
            self.assert_code("ACCESS_DENIED", "read_file", {"path": name})
            self.assert_code("ACCESS_DENIED", "write_file", {"path": name, "content": "replaced"})
            batch = call_tool("read_files", self.ctx, {"paths": [name]}).structured
            self.assertEqual(batch["files"][0]["code"], "ACCESS_DENIED")
        self.write(".env.example", "PUBLIC_FIXTURE_MARKER")
        self.assertIn("PUBLIC_FIXTURE_MARKER", call_tool("read_file", self.ctx, {"path": ".env.example"}).text)
        self.assertEqual(call_tool("search_text", self.ctx, {"pattern": "PRIVATE_FIXTURE_MARKER"}).structured["count"], 0)
        names = call_tool("find_files", self.ctx, {}).structured["files"]
        self.assertNotIn(".env", names)

    def test_custom_policy_is_fixed_until_restart_and_applies_to_history(self):
        self.write(".chatcodeignore", "# local-only material\nconfidential/\n*.local.txt\n")
        self.write("confidential/data.txt", "private")
        self.write("notes.local.txt", "private")
        changed = call_tool("write_file", self.ctx, {"path": "old.local.txt", "content": "OLD_PRIVATE"}).structured
        self.ws = Workspace(self.root, allow_sensitive_files=True)
        self.ctx.workspace = self.ws
        for name in ("confidential/data.txt", "notes.local.txt"):
            self.assert_code("ACCESS_DENIED", "read_file", {"path": name})
        if os.name == "nt":
            self.assert_code("ACCESS_DENIED", "read_file", {"path": "NOTES.LOCAL.TXT"})
        self.assert_code("ACCESS_DENIED", "get_changes", {"change_id": changed["change_id"]})
        (self.root / ".chatcodeignore").write_text("", encoding="utf-8")
        self.assert_code("ACCESS_DENIED", "read_file", {"path": "notes.local.txt"})
        self.ctx.workspace = Workspace(self.root)
        self.assertIn("private", call_tool("read_file", self.ctx, {"path": "notes.local.txt"}).text)

    def test_explicit_sensitive_opt_in_and_invalid_policy(self):
        self.write(".env", "DUMMY=1")
        self.ctx.workspace = Workspace(self.root, allow_sensitive_files=True)
        self.assertIn("DUMMY=1", call_tool("read_file", self.ctx, {"path": ".env"}).text)
        self.write(".chatcodeignore", "!allow.txt")
        with self.assertRaises(ValueError):
            Workspace(self.root)

    def test_read_only_blocks_every_mutating_tool_and_hides_them(self):
        self.settings.read_only = True
        for name in TOOLS:
            if not is_read_only_tool(name):
                self.assert_code("READ_ONLY", name, {})
        server = create_server(self.settings)
        self.addCleanup(server.server_close)
        self.assertFalse(server.settings.allow_exec)
        handler = object.__new__(MCPHandler)
        handler.server = server
        names = {item["name"] for item in handler._dispatch("tools/list", {})["tools"]}
        self.assertIn("read_file", names)
        self.assertNotIn("write_file", names)
        self.assertNotIn("exec_command", names)
        args = build_arg_parser().parse_args(["--workspace", str(self.root), "--read-only"])
        self.assertFalse(settings_from_args(args).allow_exec)

    def test_git_diff_pages_preserve_unicode_and_detect_changes(self):
        self.init_git()
        path = self.write("测试.txt", "old\n")
        self.commit()
        path.write_text("汉字😀" * 8000 + "\n", encoding="utf-8")
        first = call_tool("git_diff", self.ctx, {"max_bytes": 257}).structured
        self.assertLessEqual(len(first["diff"].encode("utf-8")), 257)
        self.assertTrue(first["truncated"])
        chunks, page = [first["diff"]], first
        while page["next_offset"] is not None:
            page = call_tool("git_diff", self.ctx, {"offset": page["next_offset"], "max_bytes": 10_000,
                "diff_revision": first["diff_revision"]}).structured
            chunks.append(page["diff"])
        expected = self.git("--no-pager", "-c", "color.ui=false", "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--relative").stdout.decode("utf-8")
        self.assertEqual("".join(chunks), expected)
        path.write_text("different\n", encoding="utf-8")
        self.assert_code("REVISION_MISMATCH", "git_diff", {"offset": first["next_offset"], "diff_revision": first["diff_revision"]})
        self.assert_code("MISSING_REVISION", "git_diff", {"offset": 1})

    def test_git_deleted_staged_and_sensitive_diffs(self):
        self.init_git()
        path = self.write("deleted.txt", "old fixture\n")
        self.write(".env", "OLD_SECRET\n")
        self.commit()
        path.unlink()
        self.write(".env", "NEW_SECRET_MUST_NOT_BE_RETURNED\n")
        deleted = call_tool("git_diff", self.ctx, {"path": "deleted.txt"}).structured
        self.assertIn("-old fixture", deleted["diff"])
        all_diff = call_tool("git_diff", self.ctx, {}).structured
        self.assertNotIn("SECRET", all_diff["diff"])
        self.assertEqual(all_diff["skipped_files_count"], 1)
        self.assert_code("ACCESS_DENIED", "git_diff", {"path": ".env"})
        self.git("add", "deleted.txt")
        self.assertIn("-old fixture", call_tool("git_diff", self.ctx, {"path": "deleted.txt", "staged": True}).structured["diff"])

    def test_git_diff_keeps_nested_workspace_scope(self):
        self.init_git()
        self.write("outside.txt", "old\n")
        self.write("nested/inside.txt", "old\n")
        self.commit()
        self.write("outside.txt", "OUTSIDE_DIFF_MUST_NOT_LEAK\n")
        self.write("nested/inside.txt", "inside\n")
        self.ctx.workspace = Workspace(self.root / "nested")
        result = call_tool("git_diff", self.ctx, {}).structured
        self.assertIn("+inside", result["diff"])
        self.assertNotIn("OUTSIDE_DIFF", result["diff"])

    def command(self, **extra):
        return {"argv": [sys.executable, "-B", "-c",
                "from pathlib import Path; p=Path('runs.txt'); p.open('a').write('run\\n')"],
                "operation_id": "retry-test", "yield_time_ms": 10_000, **extra}

    def test_command_retry_runs_once_and_detects_argument_reuse(self):
        first = call_tool("exec_command", self.ctx, self.command()).structured
        second = call_tool("exec_command", self.ctx, self.command(yield_time_ms=0)).structured
        self.assertEqual(first["process_id"], second["process_id"])
        self.assertTrue(second["replayed"])
        self.assertEqual((self.root / "runs.txt").read_text().splitlines(), ["run"])
        self.assert_code("OPERATION_ID_REUSED", "exec_command", self.command(timeout=42))

    def test_concurrent_retries_share_one_process(self):
        barrier = threading.Barrier(2)
        def run(_):
            barrier.wait(timeout=5)
            return call_tool("exec_command", self.ctx, self.command()).structured
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, range(2)))
        self.assertEqual(results[0]["process_id"], results[1]["process_id"])
        self.assertEqual((self.root / "runs.txt").read_text().splitlines(), ["run"])

    def test_restart_cannot_reexecute_reserved_command(self):
        call_tool("exec_command", self.ctx, self.command())
        self.ws.processes.shutdown()
        self.ws = Workspace(self.root)
        self.ctx.workspace = self.ws
        self.assert_code("OPERATION_UNAVAILABLE", "exec_command", self.command())
        self.assertEqual((self.root / "runs.txt").read_text().splitlines(), ["run"])

    def test_checks_replay_preserves_check_identity(self):
        first = call_tool("run_checks", self.ctx, self.command(name="tests")).structured
        second = call_tool("run_checks", self.ctx, self.command(name="tests")).structured
        self.assertEqual(first["process_id"], second["process_id"])
        self.assertEqual(second["check_name"], "tests")
        self.assert_code("OPERATION_ID_REUSED", "run_checks", self.command(name="lint"))


if __name__ == "__main__":
    unittest.main()
