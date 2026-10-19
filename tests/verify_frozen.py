"""验证正式 exe 内的源码、SQLite 任务链路与 multiprocessing 正则搜索。

仅在 Windows 构建后运行；不打开 GUI、不建立公网连接。
python tests/verify_frozen.py dist/v0.10.0/ChatCode启动器.exe
"""

import argparse
import json
import multiprocessing
import multiprocessing.popen_spawn_win32 as popen_win32
import multiprocessing.spawn as spawn
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types


def normalized(code):
    return code.replace(co_filename="<verified>", co_consts=tuple(
        normalized(item) if isinstance(item, types.CodeType) else item for item in code.co_consts))


PROBE = r'''
import json, os, sys, traceback, time
from pathlib import Path
output = Path(OUTPUT_PATH)
try:
    sys.path.insert(0, sys._MEIPASS)
    from chatcode import tasks as task_module
    from chatcode import __version__
    from chatcode.config import Settings
    from chatcode.workspace import Workspace
    from chatcode.tools import ToolContext, call_tool
    assert Path(task_module.__file__).is_relative_to(Path(sys._MEIPASS)), task_module.__file__
    base = output.parent
    project = base / "project"
    project.mkdir()
    os.environ["CHATCODE_DATA_DIR"] = str(base / "state")
    workspace = Workspace(project)
    ctx = ToolContext(workspace, Settings(workspace=project, tunnel="none"))
    def call(name, args):
        return call_tool(name, ctx, args).structured
    task = call("task_create", {"goal": "frozen verification", "operation_id": "frozen-task", "required_checks": ["tests"], "isolation": "copy"})
    identity = task["task_id"]
    call("write_file", {"path": "README.md", "content": "FROZEN_MARKER", "task_id": identity})
    search = call("search_text", {"pattern": "FROZEN_MARKER", "regex": True, "timeout_ms": 10000, "task_id": identity})
    assert search["count"] == 1, search
    call("task_update", {"task_id": identity, "expected_revision": 1, "status": "paused"})
    call("task_update", {"task_id": identity, "expected_revision": 2, "status": "running", "next_steps": ["deliver"]})
    check = call("run_checks", {"task_id": identity, "name": "tests", "argv": [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "echo CHECK_OK"], "yield_time_ms": 10000, "operation_id": "frozen-check"})
    assert check["exit_code"] == 0, check
    context = call("task_context", {"task_id": identity})
    assert context["files"][0]["state"] == "unchanged", context
    assert "CHECK_OK" in context["processes"][0]["stdout_tail"], context
    done = call("task_finish", {"task_id": identity, "expected_revision": 3, "summary": "verified frozen task flow"})
    assert done["status"] == "completed", done
    assert not (project / "README.md").exists()
    preview = call("task_changes", {"task_id": identity})
    applied = call("task_apply", {"task_id": identity, "expected_revision": done["revision"], "diff_revision": preview["diff_revision"], "dry_run": False})
    assert (project / "README.md").read_text() == "FROZEN_MARKER", applied
    reverted = call("task_revert", {"task_id": identity, "expected_revision": applied["task"]["revision"], "dry_run": False})
    assert not (project / "README.md").exists(), reverted
    workspace.isolated.shutdown()
    restored = Workspace(project).tasks.get(identity)
    assert restored["status"] == "completed", restored
    assert restored["isolation"]["state"] == "reverted", restored
    result = {"passed": True, "version": __version__, "frozen_task_sqlite_process_delivery": True, "frozen_copy_apply_revert": True, "frozen_regex_worker": True, "module_from_extracted_bundle": True}
    (project / "indexed.py").write_text("def frozen_symbol(): pass\n", encoding="utf-8")
    assert call("find_symbol", {"query": "frozen_symbol"})["count"] == 1
    assert call("context_pack", {"query": "frozen_symbol", "budget_chars": 1000})["files"]
    call("task_archive", {"task_id": identity, "expected_revision": restored["revision"]})
    assert Path(call("task_export", {"task_id": identity})["path"]).is_file()
    preview = call("task_cleanup", {"older_than_days": 0})
    call("task_cleanup", {"older_than_days": 0, "dry_run": False, "plan_revision": preview["plan_revision"]})
    assert call("task_context", {"task_id": identity})["historical_only"]
    result["frozen_index_archive_export_cleanup"] = True
    from dataclasses import replace
    native_ctx = ToolContext(workspace, replace(ctx.settings, execution_mode="native"))
    native_task = call_tool("task_create", native_ctx, {"goal": "frozen native sandbox", "operation_id": "frozen-native"}).structured
    native_code = "from pathlib import Path; import urllib.request; Path('sandbox-ok.txt').write_text('OK'); opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); response=opener.open('https://www.python.org/robots.txt', timeout=20); assert response.status==200; assert response.read(32); response.close(); print('NATIVE_FROZEN_OK')"
    native_result = call_tool("exec_command", native_ctx, {"task_id": native_task["task_id"], "argv": ["python", "-c", native_code], "yield_time_ms": 10000}).structured
    deadline = time.monotonic() + 35
    while native_result["running"] and time.monotonic() < deadline:
        native_result = call_tool("read_process", native_ctx, {"task_id": native_task["task_id"], "process_id": native_result["process_id"], "wait_ms": 1000}).structured
    assert native_result["exit_code"] == 0 and "NATIVE_FROZEN_OK" in native_result["stdout"], native_result
    result["frozen_native_sandbox_with_bundled_python"] = True
    assert native_result["sandbox"]["network_capabilities"] == ["internetClient"]
    result["frozen_native_public_https"] = True
    import tkinter as tk, gc
    from chatcode.gui import App, DESKTOP_NOTICE
    from chatcode.config import build_arg_parser
    root = tk.Tk()
    root.withdraw()
    app = App(root, build_arg_parser().parse_args(["--workspace", str(project), "--tunnel", "none", "--no-exec"]))
    def labels(widget):
        return [child.cget("text") for child in widget.winfo_children() if child.winfo_class() == "TButton"] + [label for child in widget.winfo_children() for label in labels(child)]
    assert "桌面窗口设置" not in labels(root) and "暂停桌面" not in labels(root)
    assert app.desktop_var.get() == DESKTOP_NOTICE == "桌面自动化：暂不使用"
    root.destroy()
    del app, root
    gc.collect()
    result["frozen_gui_desktop_controls_hidden"] = True
    if DESKTOP_PYTHON:
        import ctypes
        from ctypes import wintypes
        from chatcode.desktop import DesktopBridge
        native = ctypes.WinDLL("user32")
        native.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        native.SendMessageW.restype = ctypes.c_ssize_t
        native.GetForegroundWindow.restype = wintypes.HWND
        native.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        bridge = DesktopBridge(Settings(workspace=project, desktop_python=DESKTOP_PYTHON, desktop_window=DESKTOP_HANDLE, desktop_input=True))
        try:
            bridge._ensure()
            if DESKTOP_GATE:
                Path(DESKTOP_GATE + ".ready").write_text(json.dumps({"handle": DESKTOP_HANDLE, "stage": "frozen_backend_ready"}))
                deadline = time.monotonic() + 90
                while not Path(DESKTOP_GATE).exists() and time.monotonic() < deadline:
                    time.sleep(.2)
            activated = native.PostMessageW(DESKTOP_HANDLE, 0x8001, 0, 0)
            import time
            time.sleep(0.2)
            result["frozen_desktop_backend_started"] = True
            try:
                for attempt in range(5):
                    try:
                        observed, blocks = bridge.observe((str(project), ""))
                        break
                    except Exception as exc:
                        if getattr(exc, "code", "") != "USER_TAKEOVER" or attempt == 4:
                            raise
                        time.sleep(.4)
                result["frozen_readonly_observation_retries"] = attempt
            except Exception as exc:
                if getattr(exc, "code", "") not in ("WINDOW_NOT_FOREGROUND", "USER_TAKEOVER"):
                    raise
                observed = None
                result["frozen_desktop_foreground_guard"] = True
                result["frozen_real_desktop_input_tested"] = False
                result["frozen_desktop_input_block_reason"] = exc.code
            if observed is not None:
                assert blocks[0]["mimeType"] == "image/png"
                edit = next(item for item in observed["observation"]["elements"] if item["role"] == "EditControl")
                bridge.action((str(project), ""), {"action": "set_text", "element_id": edit["element_id"], "text": "frozen UI verified", "operation_id": "frozen-ui",
                    "snapshot_token": observed["snapshot_token"], "revision": observed["observation"]["revision"]})
                observed, _ = bridge.observe((str(project), ""), False)
                assert any(item.get("value") == "frozen UI verified" for item in observed["observation"]["elements"]), observed
                button = next(item for item in observed["observation"]["elements"] if item["role"] == "ButtonControl" and item["name"] == "Test action")
                bridge.action((str(project), ""), {"action": "click", "element_id": button["element_id"], "operation_id": "frozen-ui-click",
                    "snapshot_token": observed["snapshot_token"], "revision": observed["observation"]["revision"]})
                observed, blocks = bridge.observe((str(project), ""), True)
                assert any(item.get("name") == "Clicked 1" for item in observed["observation"]["elements"]), observed
                import base64
                output.with_suffix(".png").write_bytes(base64.b64decode(blocks[0]["data"]))
                result["frozen_real_desktop_input_tested"] = True
                result["frozen_real_desktop_click_and_reobserve"] = True
        finally:
            bridge.close()
except BaseException:
    result = {"passed": False, "traceback": traceback.format_exc()}
output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("exe", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--desktop-python", default="")
    parser.add_argument("--desktop-gate", default="", help="显式人工/工具激活测试窗口后创建此文件；不绕过前台保护")
    args = parser.parse_args()
    from PyInstaller.archive.readers import CArchiveReader
    executable = args.exe.resolve()
    bundle = CArchiveReader(str(executable))
    archive = bundle.open_embedded_archive("PYZ.pyz")
    root = Path(__file__).resolve().parent.parent
    modules = []
    for path in sorted((root / "chatcode").glob("*.py")):
        name = "chatcode" if path.stem == "__init__" else "chatcode." + path.stem
        if path.stem == "__main__":
            continue
        assert name in archive.toc, f"Missing packaged module: {name}"
        assert normalized(archive.extract(name)) == normalized(compile(path.read_bytes(), str(path), "exec")), name
        modules.append(name)
    worker_entry = next(name for name in bundle.toc if name.replace("\\", "/") == "desktop_backend/desktop_worker.py")
    assert bundle.extract(worker_entry) == (root / "chatcode" / "desktop_worker.py").read_bytes()
    startup = subprocess.run([str(executable), "--version"], capture_output=True, timeout=25,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    assert startup.returncode == 0, startup
    saved = (sys.executable, getattr(sys, "frozen", None), spawn.WINEXE, popen_win32.WINENV, spawn.get_executable())
    with tempfile.TemporaryDirectory(prefix="chatcode-frozen-") as directory:
        output = Path(directory) / "result.json"
        fixture, desktop_handle = None, 0
        if args.desktop_python:
            ready = Path(directory) / "window.json"
            fixture = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("desktop_fixture.py")), str(ready)], creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 8
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            desktop_handle = json.loads(ready.read_text())["handle"]
        try:
            sys.executable, sys.frozen = str(executable), True
            spawn.WINEXE, popen_win32.WINENV = True, False
            multiprocessing.set_executable(str(executable))
            script = "OUTPUT_PATH = " + repr(str(output)) + "\nDESKTOP_PYTHON = " + repr(args.desktop_python) + "\nDESKTOP_HANDLE = " + repr(desktop_handle) + "\nDESKTOP_GATE = " + repr(args.desktop_gate) + "\n" + PROBE
            process = multiprocessing.get_context("spawn").Process(target=exec, args=(script, {"__name__": "__frozen_probe__"}))
            process.start()
            process.join(150 if args.desktop_gate else 60)
            if process.is_alive():
                process.terminate()
                process.join(3)
                raise AssertionError("Frozen worker timed out")
            assert process.exitcode == 0, process.exitcode
            result = json.loads(output.read_text(encoding="utf-8"))
            assert result["passed"], result
            if args.output and output.with_suffix(".png").exists():
                import shutil
                shutil.copy2(output.with_suffix(".png"), args.output.with_suffix(".png"))
        finally:
            sys.executable, frozen, spawn.WINEXE, popen_win32.WINENV, original_executable = saved
            if frozen is None:
                del sys.frozen
            else:
                sys.frozen = frozen
            multiprocessing.set_executable(original_executable)
            if fixture is not None:
                fixture.terminate()
                fixture.wait(5)
    result.update(packaged_modules_match_source=len(modules), packaged_desktop_worker_source_matches=True, packaged_version_startup_exit_code=startup.returncode)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
