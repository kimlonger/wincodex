"""LSP framing、UTF-16、事务提交与无语言服务时的明确降级。"""

import argparse
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from chatcode.config import Settings, build_arg_parser, parse_language_server
from chatcode.files import FileError, read_text_file
from chatcode.gui_profiles import load_profile, save_profile
from chatcode.lsp import offset, position, services, utf16_index
from chatcode.tools import ToolContext, ToolError, call_tool
from chatcode.workspace import Workspace


class LanguageCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatcode-lsp-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "project"
        self.root.mkdir()
        env = patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(self.root.parent / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.ctx = ToolContext(Workspace(self.root), Settings(workspace=self.root, tunnel="none"))
        self.addCleanup(lambda: services(self.ctx).close())

    def configure(self, mode="normal", language="python"):
        fixture = Path(__file__).with_name("lsp_fixture.py")
        self.ctx.settings.language_servers[language] = [sys.executable, "-B", str(fixture), mode]

    def put(self, name, text):
        path = self.root / name
        path.write_bytes(text.encode("utf-8"))
        return read_text_file(path).revision

    def call(self, name, **args):
        return call_tool(name, self.ctx, args).structured

    def test_utf16_surrogate_positions_and_invalid_boundaries(self):
        text = 's="😀"; foo()\n'
        self.assertEqual(position(text, 1, 8), {"line": 0, "character": 8})
        self.assertEqual(offset(text, position(text, 1, 8)), 7)
        with self.assertRaises(FileError):
            utf16_index("😀", 1)
        with self.assertRaises(FileError):
            offset(text, {"line": True, "character": 0})
        self.assertEqual(offset("x", {"line": 1, "character": 0}, allow_eof=True), 1)
        with self.assertRaises(FileError):
            offset("x", {"line": 2, "character": 0}, allow_eof=True)

    def test_text_fallback_is_labelled_and_paginates(self):
        self.put("a.py", "foo = 1\nprint(foo)\n# foo comment\n")
        first = self.call("find_references", path="a.py", line=1, column=1, limit=1)
        second = self.call("find_references", path="a.py", line=1, column=1, limit=1, offset=first["next_offset"])
        self.assertFalse(first["semantic"])
        self.assertEqual(first["backend"], "text_candidates")
        self.assertEqual(second["references"][0]["line"], 2)
        self.assertEqual(services(self.ctx).sessions, {})

    def test_references_use_stdio_and_unicode_columns(self):
        self.configure()
        self.put("a.py", 's="😀"; foo = 1\nprint(foo)\n')
        data = self.call("find_references", path="a.py", line=1, column=8)
        self.assertTrue(data["semantic"])
        self.assertEqual(data["references"][0]["column"], 8)
        self.assertEqual(data["count"], 2)

    def test_cross_file_rename_dry_run_replay_and_undo(self):
        self.configure()
        rev = self.put("a.py", "def foo():\n    return 1\n")
        self.put("b.py", "from a import foo\nprint(foo())\n")
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        args = dict(path="a.py", line=1, column=5, new_name="better", expected_revision=rev, operation_id="rename-1")
        self.call("rename_symbol", **args, dry_run=True)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        result = self.call("rename_symbol", **args)
        self.assertIn("better", (self.root / "b.py").read_text())
        replay = self.call("rename_symbol", **args)
        self.assertEqual(result["change_id"], replay["change_id"])
        self.call("undo_changes", change_id=result["change_id"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        session = services(self.ctx).get(self.root / "a.py")
        services(self.ctx).close()
        self.assertIsNotNone(session.process.poll())

    def test_stale_revision_rejected_before_launch(self):
        self.configure()
        self.put("a.py", "foo = 1\n")
        with self.assertRaises(ToolError) as caught:
            self.call("rename_symbol", path="a.py", line=1, column=1, new_name="bar", expected_revision="outdated")
        self.assertEqual(caught.exception.code, "REVISION_MISMATCH")
        self.assertFalse(services(self.ctx).sessions)

    def test_outside_path_and_bad_version_and_overlapping_edits_write_nothing(self):
        for mode, code in (("outside", "PATH_ESCAPE"), ("version", "REVISION_MISMATCH"), ("overlap", "OVERLAPPING_EDITS")):
            with self.subTest(mode=mode):
                services(self.ctx).close()
                self.ctx.workspace._language_services = None
                self.configure(mode)
                rev = self.put("a.py", "foo = 1\n")
                with self.assertRaises(ToolError) as caught:
                    self.call("rename_symbol", path="a.py", line=1, column=1, new_name="bar", expected_revision=rev)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual((self.root / "a.py").read_bytes(), b"foo = 1\n")

    def test_external_change_during_rename_is_preserved(self):
        self.configure("external")
        rev = self.put("a.py", "foo = 1\n")
        self.put("b.py", "print(foo)\n")
        with self.assertRaises(ToolError) as caught:
            self.call("rename_symbol", path="a.py", line=1, column=1, new_name="bar", expected_revision=rev)
        self.assertEqual(caught.exception.code, "REVISION_MISMATCH")
        self.assertEqual((self.root / "a.py").read_bytes(), b"foo = 1\n")
        self.assertEqual((self.root / "b.py").read_bytes(), b"# external edit\n")

    def test_multilanguage_symbol_read_and_exact_symbol_edit(self):
        self.configure(language="javascript")
        rev = self.put("a.js", '/*😀*/function foo() { return 1; }\nfoo();\n')
        symbol = self.call("code_outline", path="a.js")["symbols"][0]
        self.assertEqual(symbol["column"], 15)
        data = self.call("read_code", path="a.js", symbol_id=symbol["symbol_id"])
        self.assertEqual(data["content"], "function foo() { return 1; }")
        self.call("edit_files", files=[{"path": "a.js", "expected_revision": rev,
                  "edits": [{"symbol_id": symbol["symbol_id"], "new_text": "function foo() { return 2; }"}]}])
        self.assertEqual((self.root / "a.js").read_text(encoding="utf-8"), '/*😀*/function foo() { return 2; }\nfoo();\n')

    def test_noexec_prevents_language_processes(self):
        self.configure(language="javascript")
        self.ctx.settings.allow_exec = False
        self.put("a.js", "function foo() {}\n")
        with self.assertRaises(ToolError) as caught:
            self.call("code_outline", path="a.js")
        self.assertEqual(caught.exception.code, "EXEC_DISABLED")
        self.assertFalse(services(self.ctx).sessions)

    def test_language_server_configuration_and_profile_precedence(self):
        self.configure()
        save_profile(self.ctx.settings)
        args, _ = load_profile(build_arg_parser().parse_args([]))
        self.assertEqual(dict(args.language_server), self.ctx.settings.language_servers)
        explicit, _ = load_profile(build_arg_parser().parse_args(["--language-server", 'python=["custom", "--stdio"]']))
        self.assertEqual(dict(explicit.language_server), {"python": ["custom", "--stdio"]})
        self.assertEqual(parse_language_server('python="C:\\Program Files\\pylsp.exe" --stdio')[1], [r"C:\Program Files\pylsp.exe", "--stdio"])
        for invalid in ("python=[]", "python=[1]", "../python=pylsp", "python=\0"):
            with self.assertRaises(argparse.ArgumentTypeError):
                parse_language_server(invalid)
