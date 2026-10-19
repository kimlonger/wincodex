"""后台任务的增量输出、超时、取消、子进程清理和有界日志。"""

import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from chatcode.config import Settings
from chatcode.processes import _Log
from chatcode.tools import ToolContext, ToolError, call_tool
from chatcode.workspace import Workspace


class ProcessCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatcode-process-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        env = patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.ws = Workspace(self.workspace)
        self.ctx = ToolContext(self.ws, Settings(workspace=self.workspace, tunnel="none"))
        self.addCleanup(self.ws.processes.shutdown)

    def run_code(self, code, **args):
        return call_tool("exec_command", self.ctx, {"argv": [sys.executable, "-B", "-c", code], **args})

    def test_short_command_argv_unicode_and_environment(self):
        result = self.run_code("import os; print('你好'); print(os.getenv('CHATCODE_TEST_VALUE'))", env={"CHATCODE_TEST_VALUE": "test"}, yield_time_ms=10_000)
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured["exit_code"], 0)
        self.assertIn("你好", result.structured["stdout"])
        self.assertIn("test", result.structured["stdout"])

    def test_background_log_cursor_does_not_repeat_output(self):
        result = self.run_code("import time;print('FIRST',flush=True);time.sleep(0.7);print('SECOND',flush=True)", yield_time_ms=0)
        identity = result.structured["process_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            first = call_tool("read_process", self.ctx, {"process_id": identity}).structured
            if "FIRST" in first["stdout"]:
                break
            time.sleep(0.02)
        self.assertIn("FIRST", first["stdout"])
        second = call_tool("read_process", self.ctx, {"process_id": identity, "stdout_offset": first["next_stdout_offset"], "wait_ms": 3000}).structured
        self.assertIn("SECOND", second["stdout"])
        self.assertNotIn("FIRST", second["stdout"])
        self.assertFalse(second["running"])

    def test_nonzero_exit_is_a_tool_failure_with_logs(self):
        result = self.run_code("import sys;print('failure',file=sys.stderr);sys.exit(3)", yield_time_ms=10_000)
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured["exit_code"], 3)
        self.assertIn("failure", result.structured["stderr"])

    def test_timeout_cleans_up_task(self):
        result = self.run_code("import time;time.sleep(30)", timeout=1, yield_time_ms=3000)
        self.assertTrue(result.is_error)
        self.assertTrue(result.structured["timed_out"])
        self.assertEqual(result.structured["state"], "timed_out")
        self.assertFalse(result.structured["running"])

    def test_stop_cleans_up_grandchild(self):
        result = self.run_code("import subprocess,sys,time; child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);print(child.pid,flush=True);time.sleep(30)", timeout=0, yield_time_ms=1000)
        self.assertTrue(result.structured["running"])
        child_pid = int(result.structured["stdout"].strip())
        stopped = call_tool("stop_process", self.ctx, {"process_id": result.structured["process_id"]})
        self.assertFalse(stopped.is_error)
        self.assertFalse(stopped.structured["running"])
        self.assertEqual(stopped.structured["state"], "cancelled")
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x100000, False, child_pid)
            if handle:
                try:
                    self.assertNotEqual(kernel.WaitForSingleObject(handle, 3000), 0x102)
                finally:
                    kernel.CloseHandle(handle)

    def test_log_rotation_is_bounded_and_reports_lost_bytes(self):
        with patch.object(_Log, "LIMIT", 1024):
            result = self.run_code("print('X'*5000+'END')", yield_time_ms=10_000).structured
        self.assertIn("END", result["stdout"])
        self.assertGreater(result["stdout_lost_bytes"], 0)
        log = self.ws.processes.jobs[result["process_id"]]["stdout_log"]
        self.assertLessEqual(log.path.stat().st_size, 1024)

    def test_execution_disabled_and_readonly_batch_cannot_launch(self):
        self.ctx.settings.allow_exec = False
        with self.assertRaises(ToolError) as caught:
            self.run_code("print(1)")
        self.assertEqual(caught.exception.code, "EXEC_DISABLED")
        with self.assertRaises(ToolError):
            call_tool("batch_read", self.ctx, {"requests": [{"tool": "exec_command", "arguments": {"command": "echo test"}}]})

    def test_named_check_uses_argv_and_returns_diagnostics(self):
        import json
        argv = [sys.executable, "-B", "-c", "print('src/example.py:12:3: sample diagnostic')"]
        (self.workspace / ".chatcode.toml").write_text("[checks]\ntests = " + json.dumps(argv) + "\n", encoding="utf-8")
        data = call_tool("run_checks", self.ctx, {"name": "tests", "yield_time_ms": 10_000}).structured
        self.assertEqual(data["exit_code"], 0)
        self.assertEqual(data["diagnostics"][0]["line"], 12)
        self.assertEqual(data["diagnostics"][0]["column"], 3)

    def test_active_process_limit(self):
        for _ in range(4):
            self.run_code("import time;time.sleep(30)", timeout=0, yield_time_ms=0)
        with self.assertRaises(ToolError) as caught:
            self.run_code("print('fifth')", yield_time_ms=0)
        self.assertEqual(caught.exception.code, "PROCESS_LIMIT")

    def test_powershell_unicode_output(self):
        if not (shutil.which("pwsh") or shutil.which("powershell")):
            self.skipTest("没有 PowerShell")
        data = call_tool("exec_command", self.ctx, {"command": "Write-Output '你好'", "shell": "powershell", "yield_time_ms": 10_000}).structured
        self.assertEqual(data["exit_code"], 0, data)
        self.assertIn("你好", data["stdout"])


if __name__ == "__main__":
    unittest.main()
