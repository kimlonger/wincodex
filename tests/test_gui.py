"""使用隐藏窗口验证 GUI 参数和本地服务启停，不建立公网隧道。"""

from __future__ import annotations

import os
import gc
from pathlib import Path
import socket
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatcode.config import build_arg_parser


class GuiCase(unittest.TestCase):
    def tearDown(self):
        # Tk 对象包含回调环；必须在主线程回收，不能等下一次后台线程触发 GC。
        gc.collect()

    def test_refresh_requested_during_read_runs_again_after_completion(self):
        from types import SimpleNamespace
        from chatcode.gui import App
        from unittest.mock import Mock
        app = App.__new__(App)
        app.root = Mock()
        app.root.after.return_value = "timer"
        app.runtime = object()
        app._refresh_id = None
        app._refreshing = app._refresh_again = app._switching = False
        app._task_offset = 0
        app.show_archived = SimpleNamespace(get=lambda: False)
        app.desktop_var = SimpleNamespace(set=lambda _value: None)
        app.server = SimpleNamespace(projects=SimpleNamespace(desktop=SimpleNamespace(
            status=lambda: {"configured": False, "paused": True, "input_enabled": False})))
        for name in ("task_tree", "process_tree", "change_tree"):
            setattr(app, name, SimpleNamespace(selection=lambda: (), get_children=lambda: (), delete=lambda *args: None))
        pending = []
        app._background = lambda work, done: pending.append(done)
        app.refresh_panels()
        app.refresh_panels()
        app.refresh_panels()
        self.assertEqual(len(pending), 1)
        pending[0](([], [], []), None)
        self.assertEqual(len(pending), 2)
        pending[1](([], [], []), None)
        self.assertFalse(app._refreshing)
        self.assertEqual(len(pending), 2)

    def test_hidden_gui_switches_projects_without_restarting_runtime(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except (ImportError, RuntimeError) as exc:
            self.skipTest(str(exc))
        root.withdraw()
        from chatcode.gui import App
        from chatcode.tools import ToolContext, call_tool
        with tempfile.TemporaryDirectory(prefix="chatcode-gui-switch-") as directory:
            base = Path(directory)
            first, second = base / "first", base / "second"
            first.mkdir()
            second.mkdir()
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            args = build_arg_parser().parse_args(["--workspace", str(first), "--port", str(port),
                "--tunnel", "none", "--no-exec", "--no-proxy"])
            errors = []
            finished = False
            timeout_id = None
            with patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(base / "state")}):
                app = App(root, args)
                def finish(error=None):
                    nonlocal finished
                    if finished:
                        return
                    finished = True
                    if timeout_id is not None:
                        root.after_cancel(timeout_id)
                    if error:
                        errors.append(error)
                    app.stop()
                    root.destroy()
                def wait_ready():
                    try:
                        if not app.public_url:
                            root.after(30, wait_ready)
                            return
                        runtime, server = app.runtime, app.server
                        url, http = app.settings.mcp_url(), app.runtime._http_thread
                        changed = call_tool("write_file", ToolContext(server.workspace, server.settings),
                            {"path": "first-only.txt", "content": "first"}).structured
                        task = call_tool("task_create", ToolContext(server.workspace, server.settings),
                            {"goal": "保留项目 A 的任务", "operation_id": "gui-task"}).structured
                        app.workspace_var.set(str(second))
                        app.switch_workspace()
                        def wait_second():
                            try:
                                if app._switching:
                                    root.after(30, wait_second)
                                    return
                                self.assertEqual(app.server.workspace.root, second.resolve())
                                self.assertIs(app.runtime, runtime)
                                self.assertIs(app.server, server)
                                self.assertIs(app.runtime._http_thread, http)
                                self.assertEqual(app.settings.mcp_url(), url)
                                self.assertFalse(app.change_tree.exists(changed["change_id"]))
                                self.assertFalse(app.task_tree.exists(task["task_id"]))
                                app.workspace_var.set(str(first))
                                app.switch_workspace()
                                def wait_first():
                                    try:
                                        if app._switching or app._refreshing:
                                            root.after(30, wait_first)
                                            return
                                        self.assertEqual(app.server.workspace.root, first.resolve())
                                        self.assertTrue(app.change_tree.exists(changed["change_id"]))
                                        self.assertTrue(app.task_tree.exists(task["task_id"]))
                                        app.task_tree.selection_set(task["task_id"])
                                        app.update_task("paused")
                                        def wait_paused():
                                            try:
                                                if server.workspace.tasks.get(task["task_id"])["status"] != "paused":
                                                    root.after(30, wait_paused)
                                                    return
                                                finish()
                                            except Exception as exc:
                                                finish(exc)
                                        root.after(30, wait_paused)
                                    except Exception as exc:
                                        finish(exc)
                                root.after(30, wait_first)
                            except Exception as exc:
                                finish(exc)
                        root.after(30, wait_second)
                    except Exception as exc:
                        finish(exc)
                def start():
                    try:
                        app.start()
                        root.after(30, wait_ready)
                    except Exception as exc:
                        finish(exc)
                root.after(0, start)
                timeout_id = root.after(8000, lambda: finish(AssertionError("GUI 切换超时")))
                try:
                    root.mainloop()
                finally:
                    if app.runtime is not None:
                        app.runtime.stop()
        self.assertFalse(errors, errors)

    def test_packaged_entry_forwards_explicit_arguments(self):
        with patch.object(sys, "argv", ["ChatCode启动器.exe", "--port", "9988", "--no-exec", "--tunnel", "none"]), \
                patch("chatcode.gui.run_gui", return_value=0) as launch:
            with self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(Path(__file__).resolve().parent.parent / "gui_launcher.py"), run_name="__main__")
        self.assertEqual(stopped.exception.code, 0)
        args = launch.call_args.args[0]
        self.assertEqual(args.port, 9988)
        self.assertTrue(args.no_exec)
        self.assertEqual(args.tunnel, "none")

    def test_hidden_gui_honors_args_and_stops_local_service(self):
        try:
            import tkinter as tk
        except ImportError as exc:
            self.skipTest(f"缺少 Tk：{exc}")
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"没有可用的 Tk 显示环境：{exc}")
        root.withdraw()
        from chatcode.gui import App
        from chatcode.tools import ToolContext, call_tool

        with tempfile.TemporaryDirectory(prefix="chatcode-gui-test-") as directory:
            workspace = Path(directory) / "project"
            workspace.mkdir()
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            args = build_arg_parser().parse_args(["--workspace", str(workspace), "--port", str(port),
                                                  "--auth-mode", "bearer", "--tunnel", "none", "--no-exec", "--no-proxy"])
            errors, checks, previews = [], [], []
            timeout_id = None
            runtime = None
            with patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(Path(directory) / "state")}):
                app = App(root, args)
                app._show_text = lambda title, text: previews.append(text)
                def finish(error=None):
                    if timeout_id is not None:
                        root.after_cancel(timeout_id)
                    if error is not None:
                        errors.append(error)
                        import faulthandler
                        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
                    try:
                        app.stop()
                    finally:
                        root.destroy()
                def inspect():
                    nonlocal runtime
                    try:
                        if app.server is not None and app.public_url:
                            runtime = app.runtime
                            self.assertEqual(app.server.settings.workspace, workspace.resolve())
                            self.assertEqual(app.server.settings.auth_mode, "bearer")
                            self.assertFalse(app.server.settings.allow_exec)
                            self.assertFalse(app.server.settings.auto_proxy)
                            self.assertIn("仅本地", app.status_var.get())
                            self.assertEqual(len(app.tabs.tabs()), 4)
                            self.assertEqual(app.desktop_var.get(), "桌面自动化：暂不使用")
                            def button_labels(widget):
                                return [child.cget("text") for child in widget.winfo_children() if child.winfo_class() == "TButton"] + [
                                    label for child in widget.winfo_children() for label in button_labels(child)]
                            self.assertNotIn("桌面窗口设置", button_labels(root))
                            self.assertNotIn("暂停桌面", button_labels(root))
                            self.assertEqual(str(app.workspace_input.cget("state")), "normal")
                            self.assertEqual(str(app.switch_btn.cget("state")), "normal")
                            checks.append(True)
                            context = ToolContext(app.server.workspace, app.server.settings)
                            changed = call_tool("write_file", context, {"path": "gui.txt", "content": "GUI integration"}).structured
                            app.refresh_panels()
                            def inspect_changes():
                                try:
                                    identity = changed["change_id"]
                                    if not app.change_tree.exists(identity):
                                        root.after(30, inspect_changes)
                                        return
                                    app.change_tree.selection_set(identity)
                                    app.show_change()
                                    def undo_after_preview():
                                        try:
                                            if not previews:
                                                root.after(30, undo_after_preview)
                                                return
                                            self.assertIn("GUI integration", previews[0])
                                            app.undo_selected_change()
                                            def inspect_undo():
                                                if (workspace / "gui.txt").exists():
                                                    root.after(30, inspect_undo)
                                                    return
                                                checks.append("diff and undo")
                                                task = call_tool("task_create", context, {"goal": "GUI copy flow", "operation_id": "gui-copy", "isolation": "copy"}).structured
                                                call_tool("write_file", context, {"task_id": task["task_id"], "path": "copy.txt", "content": "copy content"})
                                                call_tool("task_finish", context, {"task_id": task["task_id"], "expected_revision": 1, "summary": "ready"})
                                                self.assertFalse((workspace / "copy.txt").exists())
                                                app.refresh_panels()
                                                def apply_copy():
                                                    try:
                                                        if not app.task_tree.exists(task["task_id"]):
                                                            root.after(30, apply_copy)
                                                            return
                                                        app.task_tree.selection_set(task["task_id"])
                                                        app.isolated_task_action("task_apply")
                                                        def revert_copy():
                                                            try:
                                                                if (not (workspace / "copy.txt").exists()
                                                                        or context.workspace.tasks.get(task["task_id"])["isolation"]["state"] != "applied"):
                                                                    root.after(30, revert_copy)
                                                                    return
                                                                self.assertEqual((workspace / "copy.txt").read_text(), "copy content")
                                                                app.isolated_task_action("task_revert")
                                                                def check_reverted():
                                                                    if (workspace / "copy.txt").exists():
                                                                        root.after(30, check_reverted)
                                                                        return
                                                                    checks.append("copy apply and revert")
                                                                    finish()
                                                                root.after(30, check_reverted)
                                                            except Exception as exc:
                                                                finish(exc)
                                                        root.after(30, revert_copy)
                                                    except Exception as exc:
                                                        finish(exc)
                                                root.after(30, apply_copy)
                                            root.after(30, inspect_undo)
                                        except Exception as exc:
                                            finish(exc)
                                    root.after(30, undo_after_preview)
                                except Exception as exc:
                                    finish(exc)
                            root.after(30, inspect_changes)
                        else:
                            root.after(30, inspect)
                    except Exception as exc:
                        finish(exc)
                def start():
                    try:
                        app.start()
                        root.after(30, inspect)
                    except BaseException as exc:
                        finish(exc)
                root.after(0, start)
                # 包含多轮异步刷新、落盘及副本合入/撤回，慢磁盘也必须给完整链路留出时间。
                timeout_id = root.after(15000, lambda: finish(AssertionError(
                    f"GUI 验收超时：已完成 {checks!r}；服务就绪 {bool(app.public_url)}；"
                    f"刷新中 {app._refreshing}；预览数 {len(previews)}；"
                    f"副本文件存在 {(workspace / 'copy.txt').exists()}")))
                try:
                    # Hidden acceptance must report modal errors, never block behind an invisible dialog.
                    with patch("chatcode.gui.messagebox.showerror", side_effect=lambda title, message, **kw: finish(AssertionError(message))):
                        root.mainloop()
                finally:
                    if app.runtime is not None:
                        app.runtime.stop()
        self.assertFalse(errors, errors)
        self.assertTrue(checks)
        self.assertIsNotNone(runtime)
        self.assertFalse(runtime._http_thread.is_alive())
        self.assertFalse(runtime._monitor_thread.is_alive())


if __name__ == "__main__":
    unittest.main()
