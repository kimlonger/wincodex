"""可选真实集成验证：python -B tests/verify_language_service.py --python <含 pylsp 的 python>。"""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatcode.config import Settings
from chatcode.files import read_text_file
from chatcode.lsp import services
from chatcode.tools import ToolContext, call_tool
from chatcode.workspace import Workspace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True, help="已安装 python-lsp-server 的 Python")
    args = parser.parse_args()
    summary = []
    with tempfile.TemporaryDirectory(prefix="chatcode-real-pylsp-") as directory:
        root = Path(directory) / "project"
        root.mkdir()
        with patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(Path(directory) / "state")}):
            ctx = ToolContext(Workspace(root), Settings(workspace=root, tunnel="none", language_servers={"python": [args.python, "-m", "pylsp"]}))
            def call(name, **arguments):
                return call_tool(name, ctx, arguments).structured
            try:
                for newline in (True, False):
                    (root / "a.py").write_bytes(b"def foo():\n    return 1" + (b"\n" if newline else b""))
                    (root / "b.py").write_bytes(b"from a import foo\nvalue = foo()" + (b"\n" if newline else b""))
                    (root / "unrelated.py").write_bytes(b"def foo():\n    return 99\n")
                    before = {p.name: p.read_bytes() for p in root.glob("*.py")}
                    outline = call("code_outline", path="a.py", backend="lsp")
                    assert any(item["name"] == "foo" for item in outline["symbols"]), outline
                    refs = call("find_references", path="a.py", line=1, column=5)
                    assert refs["semantic"] and {item["path"] for item in refs["references"]} == {"a.py", "b.py"}, refs
                    args_ = dict(path="a.py", line=1, column=5, new_name="improved", expected_revision=read_text_file(root / "a.py").revision)
                    call("rename_symbol", **args_, dry_run=True)
                    assert before == {p.name: p.read_bytes() for p in root.glob("*.py")}
                    changed = call("rename_symbol", **args_)
                    assert b"improved" in (root / "b.py").read_bytes()
                    assert (root / "unrelated.py").read_bytes() == before["unrelated.py"]
                    call("undo_changes", change_id=changed["change_id"])
                    assert before == {p.name: p.read_bytes() for p in root.glob("*.py")}
                    summary.append({"final_newline": newline, "semantic_references": refs["count"], "cross_file_rename": True,
                                    "same_name_other_scope_preserved": True, "dry_run": True, "undo_byte_exact": True})
            finally:
                services(ctx).close()
    print(json.dumps({"language_service": "python-lsp-server", "checks": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
