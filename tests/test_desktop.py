"""桌面桥协议、任务关联、权限与动作去重；不操作真实桌面。"""

import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from chatcode.config import Settings
from chatcode.desktop import DesktopBridge
from chatcode.mcp_http import create_server, MCPHandler
from chatcode.tools import ToolContext, ToolError, call_tool
from chatcode.workspace import Workspace
from chatcode.desktop_worker import Worker


class FakeBackend:
    def __init__(self, python):
        self.calls, self.actions, self.closed = [], 0, False
        self.fail, self.bad_image = None, False

    def request(self, method, args):
        self.calls.append((method, args))
        if self.fail:
            raise ToolError(self.fail, "injected backend error")
        if method == "bind":
            return {"handle": args["handle"], "title": "fixture", "identity": [123, 1]}
        if method == "windows":
            return {"windows": [{"handle": 7, "title": "fixture"}]}
        if method == "observe":
            image = b"invalid" if self.bad_image else b"\x89PNG\r\n\x1a\nfixture"
            return {"state": {"revision": "a" * 64, "elements": [{"element_id": "0", "role": "EditControl"}]},
                    "content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(image).decode()}] if args.get("image") else []}
        if method == "action":
            self.actions += 1
            return {"performed": True, "verification_required": True}
        return {"released": True}

    def close(self):
        self.closed = True


class DesktopCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="chatcode-desktop-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.settings = Settings(workspace=self.root, desktop_python="fixture-python", desktop_window=7, desktop_input=True)
        self.manager = DesktopBridge(self.settings, FakeBackend)
        self.addCleanup(self.manager.close)
        self.ctx = ToolContext(Workspace(self.root), self.settings, registry=SimpleNamespace(desktop=self.manager))
        self.scope = (str(self.root), "")

    def call(self, name, args=None, ctx=None):
        return call_tool(name, ctx or self.ctx, args or {})

    def args(self, observation, **extra):
        return {"snapshot_token": observation["snapshot_token"], "revision": observation["observation"]["revision"],
                "element_id": "0", "action": "click", "operation_id": "click-1", **extra}

    def error(self, code, callback):
        with self.assertRaises(ToolError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)

    def test_disabled_default_and_readonly_cannot_input(self):
        disabled = DesktopBridge(Settings(workspace=self.root), FakeBackend)
        self.error("DESKTOP_PAUSED", lambda: disabled.observe(self.scope))
        self.assertIsNone(disabled.backend)
        readonly = replace(self.ctx, settings=replace(self.settings, read_only=True))
        self.error("READ_ONLY", lambda: self.call("desktop_action", {}, readonly))
        self.assertTrue(self.call("desktop_observe", ctx=readonly).content)

    def test_image_result_survives_mcp_serialization(self):
        server = create_server(replace(self.settings, port=0))
        self.addCleanup(server.server_close)
        server.projects.desktop.close()
        server.projects.desktop = self.manager
        handler = object.__new__(MCPHandler)
        handler.server = server
        result = handler._dispatch("tools/call", {"name": "desktop_observe", "arguments": {}})
        self.assertEqual([item["type"] for item in result["content"]], ["text", "image"])
        self.assertEqual(result["content"][1]["mimeType"], "image/png")
        self.assertIn("snapshot_token", result["structuredContent"])

    def test_ticket_consumed_and_network_retry_is_not_repeated(self):
        observation, _ = self.manager.observe(self.scope)
        args = self.args(observation)
        self.assertTrue(self.manager.action(self.scope, args)["performed"])
        self.assertTrue(self.manager.action(self.scope, args)["replayed"])
        self.assertEqual(self.manager.backend.actions, 1)
        self.error("DESKTOP_STALE", lambda: self.manager.action(self.scope, {**args, "operation_id": "new"}))
        self.error("OPERATION_ID_REUSED", lambda: self.manager.action(self.scope, {**args, "action": "set_text", "text": "changed"}))

    def test_scope_and_lease_expiration(self):
        self.manager.observe(self.scope)
        self.error("DESKTOP_PROJECT_DENIED", lambda: self.manager.observe((str(self.root / "other"), "")))
        self.error("DESKTOP_IN_USE", lambda: self.manager.observe((str(self.root), "other-task")))
        self.manager.expires = 0
        self.assertTrue(self.manager.observe((str(self.root), "other-task"))[0]["snapshot_token"])

    def test_observation_without_input_permission_and_invalid_image(self):
        self.manager.input = False
        observed, _ = self.manager.observe(self.scope)
        self.error("DESKTOP_INPUT_DISABLED", lambda: self.manager.action(self.scope, self.args(observed)))
        self.manager.backend.bad_image = True
        self.error("DESKTOP_PROTOCOL_ERROR", lambda: self.manager.observe(self.scope))

    def test_pause_interrupts_and_cannot_be_undone_remotely(self):
        self.manager.observe(self.scope)
        backend = self.manager.backend
        self.manager.lock.acquire()
        try:
            self.manager.pause()
        finally:
            self.manager.lock.release()
        self.assertTrue(backend.closed)
        self.error("DESKTOP_PAUSED", lambda: self.manager.observe(self.scope))
        self.manager.bind_local("fixture-python", 7, self.root, True)
        self.assertFalse(self.manager.paused)

    def test_timeout_reservation_never_retries_input(self):
        observed, _ = self.manager.observe(self.scope)
        args = self.args(observed)
        self.manager.backend.fail = "DESKTOP_TIMEOUT"
        self.error("DESKTOP_TIMEOUT", lambda: self.manager.action(self.scope, args))
        self.assertTrue(self.manager.paused)
        self.manager.bind_local("fixture-python", 7, self.root, True)
        self.error("OPERATION_UNAVAILABLE", lambda: self.manager.action(self.scope, args))
        self.assertEqual(self.manager.backend.actions, 0)

    def test_restart_old_operation_is_not_reexecuted(self):
        observed, _ = self.manager.observe(self.scope)
        args = self.args(observed)
        self.manager.action(self.scope, args)
        restored = DesktopBridge(self.settings, FakeBackend)
        self.addCleanup(restored.close)
        self.error("OPERATION_UNAVAILABLE", lambda: restored.action(self.scope, args))
        self.assertIsNone(restored.backend)

    def test_task_requires_post_action_observation_and_records_no_images_or_text(self):
        task = self.call("task_create", {"goal": "UI test", "operation_id": "task"}).structured
        task_id = task["task_id"]
        observed = self.call("desktop_observe", {"task_id": task_id}).structured
        self.call("desktop_action", {**self.args(observed, action="set_text", text="PRIVATE_TYPED_VALUE"), "task_id": task_id})
        self.error("TASK_DESKTOP_UNVERIFIED", lambda: self.call("task_finish", {"task_id": task_id, "expected_revision": 1, "summary": "done"}))
        self.call("desktop_observe", {"task_id": task_id, "image": False})
        completed = self.call("task_finish", {"task_id": task_id, "expected_revision": 1, "summary": "done"}).structured
        self.assertEqual(completed["status"], "completed")
        events = json.dumps(self.ctx.workspace.tasks.events(task_id), ensure_ascii=False)
        self.assertNotIn("PRIVATE_TYPED_VALUE", events)
        self.assertNotIn("base64", events)

    def test_batch_observation_is_rejected_and_does_not_drop_images(self):
        self.error("DIRECT_CALL_REQUIRED", lambda: self.call("batch_read", {"requests": [{"tool": "desktop_observe", "arguments": {}}]}))
        self.assertIsNone(self.manager.backend)

    def test_local_window_discovery_rebind_and_project_scope(self):
        items = self.manager.local_windows("another-python")
        self.assertEqual(items[0]["handle"], 7)
        self.assertTrue(self.manager.paused)
        self.manager.bind_local("another-python", 7, self.root / "B")
        self.error("DESKTOP_PROJECT_DENIED", lambda: self.manager.observe(self.scope))

    def test_concurrent_request_is_busy(self):
        self.manager.lock.acquire()
        try:
            self.error("DESKTOP_BUSY", lambda: self.manager.observe(self.scope))
            self.error("DESKTOP_BUSY", lambda: self.manager.release(self.scope))
        finally:
            self.manager.lock.release()

    def test_worker_expiration_does_not_require_another_client_request(self):
        worker = Worker.__new__(Worker)
        released = []
        worker.native = SimpleNamespace(locked=True, release=lambda: released.append(True))
        worker.until, worker.state = 0, {"revision": "old"}
        worker.expire()
        self.assertEqual(released, [True])
        self.assertIsNone(worker.state)

    def test_background_observation_does_not_authorize_background_input(self):
        from chatcode.desktop_worker import Native, Rejected
        native = Native.__new__(Native)
        native.identity = lambda _handle: [1, 2]
        native.user = SimpleNamespace(GetForegroundWindow=lambda: 99, GetAsyncKeyState=lambda _key: 0)
        native.check(7, [1, 2], foreground=False)
        with self.assertRaises(Rejected) as caught:
            native.check(7, [1, 2])
        self.assertEqual(caught.exception.code, "WINDOW_NOT_FOREGROUND")
        flags = []
        worker = Worker.__new__(Worker)
        worker.handle, worker.identity = 7, [1, 2]
        worker.native = SimpleNamespace(acquire=lambda: None, input_stamp=lambda: 1,
            check=lambda _h, _i, **kw: flags.append(kw.get("foreground", True)))
        worker.tree = lambda **kw: (flags.append(kw.get("foreground", True)) or ({"revision": "fresh"}, {}))
        worker.observe(False)
        self.assertEqual(flags, [False, False])
        native.user.GetAsyncKeyState = lambda _key: 0x8000
        with self.assertRaises(Rejected) as caught:
            native.check(7, [1, 2], foreground=False)
        self.assertEqual(caught.exception.code, "USER_TAKEOVER")
