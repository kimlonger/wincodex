"""大目录的索引构建、热查询与增量修改验收；只在临时目录生成代码。"""

import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatcode.config import Settings
from chatcode.tools import ToolContext, call_tool
from chatcode.workspace import Workspace


def main():
    with tempfile.TemporaryDirectory(prefix="chatcode-large-index-") as directory:
        root = Path(directory) / "project"
        root.mkdir()
        for i in range(6000):
            path = root / f"包{i // 100}" / f"source{i}.py"
            path.parent.mkdir(exist_ok=True)
            path.write_text(f"def symbol{i}():\n    return {i}\n", encoding="utf-8")
        with patch.dict(os.environ, CHATCODE_DATA_DIR=str(Path(directory) / "state")):
            ctx = ToolContext(Workspace(root), Settings(workspace=root))
            started = time.monotonic()
            batches = 0
            while True:
                data = call_tool("refresh_index", ctx, {"max_files": 1000}).structured
                batches += 1
                if data["complete"]:
                    break
                assert batches < 30, data
            cold = time.monotonic() - started
            started = time.monotonic()
            data = call_tool("find_symbol", ctx, {"query": "symbol5999"}).structured
            warm = time.monotonic() - started
            assert data["count"] == 1 and data["index_refresh"]["updated"] == 0, data
            (root / "包59" / "source5999.py").write_text("def renamed5999(): pass\n", encoding="utf-8")
            data = call_tool("find_symbol", ctx, {"query": "renamed5999"}).structured
            assert data["count"] == 1 and data["index_refresh"]["updated"] == 1, data
            ctx.workspace._index = None
            assert ctx.workspace.index.status()["files"] == 6000
            output = {"files": 6000, "unicode_paths": True, "cold_seconds": round(cold, 3),
                      "warm_query_seconds": round(warm, 3), "batches": batches,
                      "warm_reparsed_files": 0, "incremental_reparsed_files": 1, "restart_persistence": True}
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    text = json.dumps(output, indent=2)
    if target:
        target.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
