"""v0.3 的项目发现、按符号读取、批量编辑和撤销闭环。"""

import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from chatcode.config import Settings
from chatcode.files import read_text_file
from chatcode.tools import TOOLS, ToolContext, ToolError, call_tool
from chatcode.workspace import Workspace


class WorkflowCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatcode-workflow-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        env = patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.ctx = ToolContext(Workspace(self.workspace), Settings(workspace=self.workspace, tunnel="none"))

    def put(self, name, text):
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
        return read_text_file(path).revision

    def call(self, name, **args):
        return call_tool(name, self.ctx, args).structured

    def test_discovery_and_glob_pagination(self):
        self.put("pyproject.toml", "[project]\nname='demo'\n")
        self.put("src/a.py", "x=1\n")
        self.put("src/deep/b.py", "x=2\n")
        overview = self.call("project_overview")
        self.assertIn("python", overview["project_types"])
        direct = self.call("find_files", pattern="src/*.py")
        self.assertEqual(direct["files"], ["src/a.py"])
        first = self.call("find_files", pattern="**/*.py", limit=1)
        second = self.call("find_files", pattern="**/*.py", offset=first["next_offset"], limit=1)
        self.assertEqual(set(first["files"] + second["files"]), {"src/a.py", "src/deep/b.py"})
        self.assertIsNone(second["next_offset"])

    def test_outline_read_and_stale_symbol(self):
        self.put("a.py", "class A:\n    @staticmethod\n    def method(x: int) -> int:\n        return x + 1\n")
        data = self.call("code_outline", path="a.py")
        method = next(s for s in data["symbols"] if s["qualified_name"] == "A.method")
        code = self.call("read_code", path="a.py", symbol_id=method["symbol_id"])
        self.assertIn("@staticmethod", code["content"])
        self.assertIn("return x + 1", code["content"])
        self.assertNotIn("class A", code["content"])
        self.put("a.py", "class A:\n    def method(self):\n        return 2\n")
        with self.assertRaises(ToolError) as caught:
            self.call("read_code", path="a.py", symbol_id=method["symbol_id"])
        self.assertEqual(caught.exception.code, "REVISION_MISMATCH")

    def test_ambiguous_symbol_and_find_definition(self):
        self.put("a.py", "class A:\n    def method(self): pass\nclass B:\n    def method(self): pass\n")
        with self.assertRaises(ToolError) as caught:
            self.call("read_code", path="a.py", symbol="method")
        self.assertEqual(caught.exception.code, "AMBIGUOUS_SYMBOL")
        symbols = self.call("find_symbol", query="method")["symbols"]
        self.assertEqual({s["qualified_name"] for s in symbols}, {"A.method", "B.method"})

    def test_large_file_window_has_full_byte_revision(self):
        text = "line\n" * 100_000
        self.put("large.txt", text)
        data = self.call("read_file", path="large.txt", offset=90_001, limit=3)
        self.assertEqual(data["content"], "line\nline\nline")
        self.assertEqual(data["total_lines"], 100_000)
        self.assertEqual(data["revision"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(data["next_offset"], 90_004)

    def test_window_limits_do_not_silently_skip_later_lines(self):
        self.put("long.txt", "x" * 70_000 + "\nend\n")
        data = self.call("read_file", path="long.txt")
        self.assertTrue(data["truncated"])
        self.assertIn(1, data["truncated_lines"])
        self.assertEqual(data["next_offset"], 2)
        self.assertEqual(self.call("read_file", path="long.txt", offset=2)["content"], "end")

    def test_batch_read_preserves_order_and_per_item_errors(self):
        self.put("a.py", "def f(): return 1\n")
        results = self.call("batch_read", requests=[{"tool": "code_outline", "arguments": {"path": "a.py"}},
                                                   {"tool": "read_file", "arguments": {"path": "missing"}}])["results"]
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])
        with self.assertRaises(ToolError):
            self.call("batch_read", requests=[{"tool": "write_file", "arguments": {"path": "x", "content": "x"}}])
        self.assertFalse((self.workspace / "x").exists())

    def test_read_files_accepts_per_file_ranges(self):
        self.put("a.txt", "one\ntwo\nthree\n")
        data = self.call("read_files", requests=[{"path": "a.txt", "offset": 2, "limit": 1}])
        self.assertEqual(data["files"][0]["content"], "two")

    def test_multi_file_edits_diff_replay_and_undo(self):
        a = self.put("a.py", "def first():\n    return 1\n")
        b = self.put("b.txt", "old\n")
        files = [{"path": "a.py", "expected_revision": a, "edits": [{"old_text": "return 1", "new_text": "return 2"}]},
                 {"path": "b.txt", "expected_revision": b, "content": "new\n"}]
        changed = self.call("edit_files", files=files, operation_id="change-1")
        self.assertIn("+    return 2", changed["diff"])
        replay = self.call("edit_files", files=files, operation_id="change-1")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["change_id"], changed["change_id"])
        self.assertEqual(self.call("get_changes")["count"], 1)
        self.call("undo_changes", change_id=changed["change_id"])
        self.assertEqual((self.workspace / "a.py").read_text(), "def first():\n    return 1\n")
        self.assertEqual((self.workspace / "b.txt").read_text(), "old\n")
        self.assertTrue(self.call("undo_changes", change_id=changed["change_id"])["replayed"])

    def test_undo_refuses_external_changes_before_any_file_is_modified(self):
        a, b = self.put("a.txt", "a"), self.put("b.txt", "b")
        changed = self.call("edit_files", files=[{"path": "a.txt", "expected_revision": a, "content": "A"},
                                                 {"path": "b.txt", "expected_revision": b, "content": "B"}])
        self.put("b.txt", "external")
        with self.assertRaises(ToolError) as caught:
            self.call("undo_changes", change_id=changed["change_id"])
        self.assertEqual(caught.exception.code, "REVISION_MISMATCH")
        self.assertEqual((self.workspace / "a.txt").read_text(), "A")
        self.assertEqual((self.workspace / "b.txt").read_text(), "external")

    def test_edit_preflight_syntax_failure_writes_nothing(self):
        a, b = self.put("a.txt", "old"), self.put("b.py", "x=1\n")
        with self.assertRaises(ToolError) as caught:
            self.call("edit_files", files=[{"path": "a.txt", "expected_revision": a, "content": "new"},
                                           {"path": "b.py", "expected_revision": b, "content": "def broken(:\n"}])
        self.assertEqual(caught.exception.code, "SYNTAX_ERROR")
        self.assertEqual((self.workspace / "a.txt").read_text(), "old")

    def test_simultaneous_ranges_use_original_snapshot(self):
        revision = self.put("a.txt", "one\ntwo\nthree\n")
        self.call("edit_files", files=[{"path": "a.txt", "expected_revision": revision, "edits": [
            {"start_line": 1, "end_line": 1, "new_text": "ONE\nEXTRA"},
            {"start_line": 3, "end_line": 3, "new_text": "THREE"}]}])
        self.assertEqual((self.workspace / "a.txt").read_text(), "ONE\nEXTRA\ntwo\nTHREE\n")

    def test_overlapping_edits_are_rejected(self):
        revision = self.put("a.txt", "abcdef\n")
        with self.assertRaises(ToolError) as caught:
            self.call("edit_files", files=[{"path": "a.txt", "expected_revision": revision, "edits": [
                {"old_text": "abc", "new_text": "A"}, {"old_text": "bcd", "new_text": "B"}]}])
        self.assertEqual(caught.exception.code, "OVERLAPPING_EDITS")

    def test_create_move_delete_and_undo_preserves_original_bytes(self):
        revision = self.put("old.txt", "original\n")
        result = self.call("edit_files", files=[{"path": "old.txt", "action": "move", "destination": "new/place.txt", "expected_revision": revision},
                                                {"path": "created.txt", "action": "create", "content": "created"}])
        self.assertFalse((self.workspace / "old.txt").exists())
        self.assertTrue((self.workspace / "new/place.txt").exists())
        self.call("undo_changes", change_id=result["change_id"])
        self.assertEqual((self.workspace / "old.txt").read_bytes(), b"original\n")
        self.assertFalse((self.workspace / "created.txt").exists())
        self.assertFalse((self.workspace / "new/place.txt").exists())

    def test_operation_id_cannot_be_reused_for_other_payload(self):
        self.call("edit_files", files=[{"path": "a.txt", "action": "create", "content": "a"}], operation_id="unique")
        with self.assertRaises(ToolError) as caught:
            self.call("edit_files", files=[{"path": "b.txt", "action": "create", "content": "b"}], operation_id="unique")
        self.assertEqual(caught.exception.code, "OPERATION_ID_REUSED")

    def test_dry_run_does_not_create_files(self):
        result = self.call("edit_files", files=[{"path": "new.py", "action": "create", "content": "x=1\n"}], dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.workspace / "new.py").exists())

    def test_search_paginates_reports_skips_and_bounds_structured_output(self):
        self.put("many.txt", ("needle " + "中" * 490 + "\n") * 200)
        (self.workspace / "binary.dat").write_bytes(b"\0needle")
        result = self.call("search_text", pattern="needle", max_matches=5000)
        self.assertLess(result["count"], 200)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["skipped_count"], 1)
        page = self.call("search_text", pattern="needle", max_matches=1, offset=result["next_offset"])
        self.assertEqual(page["matches"][0]["line"], result["count"] + 1)

    def test_project_check_suggestion_detects_pytest(self):
        self.put("pyproject.toml", "[tool.pytest.ini_options]\ntestpaths=['tests']\n")
        self.put("tests/test_demo.py", "def test_example():\n    assert 1 == 1\n")
        self.assertEqual(self.call("project_overview")["suggested_checks"][0]["command"], "python -m pytest -q")


if __name__ == "__main__":
    unittest.main()
