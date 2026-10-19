"""显式运行真实桌面后端，仅操作本脚本创建的测试窗口。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import threading
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatcode.config import Settings
from chatcode.desktop import DesktopBridge
from chatcode.mcp_http import create_server
from chatcode.tool_types import ToolError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="chatcode-desktop-verify-") as directory:
        base = Path(directory)
        ready = base / "ready.json"
        fixture = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("desktop_fixture.py")), str(ready)],
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        manager = None
        try:
            deadline = time.monotonic() + 8
            while not ready.exists() and time.monotonic() < deadline:
                if fixture.poll() is not None:
                    raise AssertionError("Fixture exited: " + str(fixture.returncode))
                time.sleep(0.05)
            handle = json.loads(ready.read_text())["handle"]
            import ctypes
            from ctypes import wintypes
            native = ctypes.WinDLL("user32")
            native.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            def activate_fixture():
                native.SendMessageW(handle, 0x8001, 0, 0)
            with patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(base / "data")}):
                manager = DesktopBridge(Settings(workspace=base, desktop_python=args.python, desktop_window=handle, desktop_input=True))
                def observe(image=True):
                    for attempt in range(10):
                        activate_fixture()
                        try:
                            return manager.observe((str(base), ""), image)
                        except ToolError as exc:
                            if exc.code != "USER_TAKEOVER" or attempt == 9:
                                raise
                            time.sleep(0.4)
                observed, blocks = observe()
                assert blocks and blocks[0]["mimeType"] == "image/png", observed
                edit = next(item for item in observed["observation"]["elements"] if item["role"] == "EditControl")
                action = {"action": "set_text", "element_id": edit["element_id"], "text": "verified 文本", "operation_id": "type-once",
                          "snapshot_token": observed["snapshot_token"], "revision": observed["observation"]["revision"]}
                manager.action((str(base), ""), action)
                assert manager.action((str(base), ""), action)["replayed"]
                observed, _ = observe(False)
                assert any(item.get("value") == "verified 文本" for item in observed["observation"]["elements"]), observed
                button = next(item for item in observed["observation"]["elements"] if item["name"] == "Test action")
                manager.action((str(base), ""), {"action": "click", "element_id": button["element_id"], "operation_id": "click-once",
                    "snapshot_token": observed["snapshot_token"], "revision": observed["observation"]["revision"]})
                observed, _ = observe(False)
                assert any(item["name"] == "Clicked 1" for item in observed["observation"]["elements"]), observed
                manager.release((str(base), ""))
                manager.pause()
                server = create_server(Settings(workspace=base, port=0, desktop_python=args.python, desktop_window=handle, desktop_input=True))
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    def rpc(name, arguments):
                        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}).encode()
                        request = urllib.request.Request(server.settings.mcp_url(), data=payload, headers={"Content-Type": "application/json"})
                        with opener.open(request, timeout=25) as response:
                            return json.load(response)["result"]
                    raw_rpc = rpc
                    def rpc(name, arguments):
                        for attempt in range(10):
                            if name == "desktop_observe":
                                activate_fixture()
                            response = raw_rpc(name, arguments)
                            if name != "desktop_observe" or response.get("structuredContent", {}).get("code") != "USER_TAKEOVER" or attempt == 9:
                                return response
                            time.sleep(0.4)
                    task = rpc("task_create", {"goal": "real UI HTTP", "operation_id": "real-task"})["structuredContent"]
                    observation = rpc("desktop_observe", {"task_id": task["task_id"]})
                    assert any(item["type"] == "image" for item in observation["content"]), observation
                    state = observation["structuredContent"]
                    edit = next(item for item in state["observation"]["elements"] if item["role"] == "EditControl")
                    action = rpc("desktop_action", {"task_id": task["task_id"], "action": "set_text", "text": "HTTP verified", "element_id": edit["element_id"],
                        "operation_id": "http-input", "revision": state["observation"]["revision"], "snapshot_token": state["snapshot_token"]})
                    assert not action.get("isError"), action
                    blocked = rpc("task_finish", {"task_id": task["task_id"], "expected_revision": 1, "summary": "ready"})
                    assert blocked["structuredContent"]["code"] == "TASK_DESKTOP_UNVERIFIED", blocked
                    checked = rpc("desktop_observe", {"task_id": task["task_id"], "image": False})["structuredContent"]
                    assert any(item.get("value") == "HTTP verified" for item in checked["observation"]["elements"]), checked
                    completed = rpc("task_finish", {"task_id": task["task_id"], "expected_revision": 1, "summary": "UI verified"})
                    assert completed["structuredContent"]["status"] == "completed", completed
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(3)
                result = {"passed": True, "real_backend": "windows-mcp.uia 0.8.8", "window_capture_png": True,
                    "real_value_pattern_text": True, "real_click_verified": True, "duplicate_action_not_repeated": True,
                    "release_and_local_pause": True, "real_http_image_input_and_task_verification": True, "only_self_created_window": True}
        except Exception:
            if manager is not None and manager.backend is not None:
                print(manager.backend.error_tail.decode("utf-8", "replace"), file=sys.stderr)
            raise
        finally:
            if manager is not None:
                manager.close()
            fixture.terminate()
            fixture.wait(5)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
