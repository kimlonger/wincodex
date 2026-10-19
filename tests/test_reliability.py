"""v0.2 回归：真实 HTTP、真实文件、并发与故障注入；不启动公网隧道。"""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chatcode.editing as editing
from chatcode.config import Settings, build_arg_parser
from chatcode.editing import PreparedEdit
from chatcode.files import FileError, atomic_write, bytes_revision, read_text_file
from chatcode.mcp_http import create_server
from chatcode.oauth import OAuthError, OAuthServer
from chatcode.runtime import ServiceRuntime
from chatcode.tools import ToolContext, ToolError, call_tool
from chatcode.workspace import PathRejected, Workspace


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class ReliabilityCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatcode-regression-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "project"
        self.workspace.mkdir()
        self.env = patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(self.root / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.settings = Settings(workspace=self.workspace, port=0, tunnel="none", auth_mode="path")
        self.ws = Workspace(self.workspace)
        self.ctx = ToolContext(workspace=self.ws, settings=self.settings)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def write(self, name, data):
        path = self.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def revision(self, name):
        return read_text_file(self.workspace / name).revision

    def replace(self, name, old, new, **kwargs):
        return call_tool("replace_in_file", self.ctx, {
            "path": name, "old_text": old, "new_text": new,
            "expected_revision": self.revision(name), **kwargs})

    @contextmanager
    def server(self, *, mode="path", verbose=False):
        self.settings.auth_mode, self.settings.verbose = mode, verbose
        server = create_server(self.settings)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            yield server, f"http://127.0.0.1:{self.settings.port}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def http(self, url, body=None, *, headers=None):
        request = urllib.request.Request(url, data=body, headers=headers or {})
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read(), response.headers

    def rpc(self, url, method, params=None):
        status, body, _ = self.http(url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                                   "params": params if params is not None else {}}).encode(),
                                   headers={"Content-Type": "application/json"})
        return status, json.loads(body)

    def test_path_credentials_not_exposed_by_public_routes(self):
        with self.server() as (_, base):
            for route in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-authorization-server", "/oauth/authorize"):
                status, body, _ = self.http(base + route)
                self.assertEqual(status, 404)
                self.assertNotIn(self.settings.path_token.encode(), body)
                self.assertNotIn(str(self.workspace).encode(), body)
            status, body, _ = self.http(base + "/oauth/register", b'{}')
            self.assertEqual(status, 404)
            status, body, _ = self.http(base + "/health")
            self.assertEqual(json.loads(body)["workspace"], "hidden")
            self.assertEqual(self.rpc(base + "/mcp", "tools/list")[0], 404)
            self.assertIn("result", self.rpc(base + self.settings.mcp_path(), "tools/list")[1])

    def test_access_logs_redact_path_credentials(self):
        buffer = io.StringIO()
        with redirect_stderr(buffer), self.server(verbose=True) as (_, base):
            self.rpc(base + self.settings.mcp_path(), "tools/list")
        self.assertIn("[redacted]", buffer.getvalue())
        self.assertNotIn(self.settings.path_token, buffer.getvalue())

    def test_health_also_hides_workspace_in_oauth_mode(self):
        with self.server(mode="oauth") as (_, base):
            self.assertEqual(json.loads(self.http(base + "/health")[1])["workspace"], "hidden")

    def test_invalid_json_rpc_is_a_response_not_disconnection(self):
        with self.server() as (_, base):
            url = base + self.settings.mcp_path()
            for method in ([], 7, None):
                self.assertEqual(self.rpc(url, method)[1]["error"]["code"], -32600)
            self.assertEqual(self.rpc(url, "tools/list", [])[1]["error"]["code"], -32602)

    def test_bad_content_length_and_chunked_body_rejected(self):
        with self.server() as (_, base):
            for header, value in (("Content-Length", "-1"), ("Content-Length", "bad"), ("Transfer-Encoding", "chunked")):
                connection = http.client.HTTPConnection("127.0.0.1", self.settings.port, timeout=3)
                try:
                    connection.putrequest("POST", self.settings.mcp_path())
                    connection.putheader(header, value)
                    connection.endheaders()
                    response = connection.getresponse()
                    self.assertEqual(response.status, 400)
                    response.read()
                finally:
                    connection.close()

    def test_tool_inputs_are_validated_on_server(self):
        for tool, args in (("read_file", {"path": "x", "offset": 0}),
                           ("read_file", {"path": "x", "limit": True}),
                           ("server_info", {"unknown": 1}),
                           ("read_files", {"paths": [5]}),
                           ("apply_patch", {"patch": "x", "expected_revisions": {"x": 3}})):
            with self.subTest(tool=tool, args=args), self.assertRaises(ToolError) as caught:
                call_tool(tool, self.ctx, args)
            self.assertEqual(caught.exception.code, "INVALID_ARGUMENT")

    def test_crlf_replace_preserves_bytes_and_returned_revision(self):
        path = self.write("sample.py", b"alpha\r\nbeta\r\n")
        result = self.replace("sample.py", "alpha\r\nbeta", "changed\r\nbeta")
        self.assertEqual(path.read_bytes(), b"changed\r\nbeta\r\n")
        self.assertEqual(result.structured["revision"], self.revision("sample.py"))

    def test_cr_only_file_is_read_and_written_as_physical_lines(self):
        path = self.write("sample.txt", b"one\rtwo\r")
        content = read_text_file(path)
        self.assertEqual(content.line_count, 2)
        self.assertEqual(content.text, "one\ntwo\n")
        result = call_tool("read_file", self.ctx, {"path": "sample.txt", "offset": 2, "limit": 1})
        self.assertEqual(result.structured["content"], "two")
        self.replace("sample.txt", "two", "TWO")
        self.assertEqual(path.read_bytes(), b"one\rTWO\r")

    def test_bom_and_gbk_round_trip(self):
        samples = [("bom.txt", b"\xef\xbb\xbf" + "你好\r\n".encode(), "世界"),
                   ("gbk.txt", "你好\r\n".encode("gbk"), "世界")]
        for name, raw, new in samples:
            with self.subTest(name=name):
                path = self.write(name, raw)
                before = read_text_file(path)
                result = self.replace(name, "你好", new)
                expected = (b"\xef\xbb\xbf" if before.has_bom else b"") + (new + "\r\n").encode(before.encoding)
                self.assertEqual(path.read_bytes(), expected)
                self.assertEqual(result.structured["revision"], bytes_revision(expected))

    def test_write_existing_preserves_encoding_and_requires_revision(self):
        path = self.write("gbk.txt", "你好\r\n".encode("gbk"))
        with self.assertRaises(ToolError) as caught:
            call_tool("write_file", self.ctx, {"path": "gbk.txt", "content": "世界\n"})
        self.assertEqual(caught.exception.code, "REVISION_REQUIRED")
        call_tool("write_file", self.ctx, {"path": "gbk.txt", "content": "世界\n", "expected_revision": self.revision("gbk.txt")})
        self.assertEqual(path.read_bytes(), "世界\r\n".encode("gbk"))

    def test_unencodable_edit_leaves_original_unchanged(self):
        raw = "你好\r\n".encode("gbk")
        path = self.write("gbk.txt", raw)
        with self.assertRaises(ToolError) as caught:
            self.replace("gbk.txt", "你好", "😀")
        self.assertEqual(caught.exception.code, "ENCODING_ERROR")
        self.assertEqual(path.read_bytes(), raw)

    def test_raw_revision_detects_bom_and_newline_only_changes(self):
        path = self.write("file.txt", b"same\n")
        revisions = [self.revision("file.txt")]
        for raw in (b"same\r\n", b"\xef\xbb\xbfsame\n"):
            path.write_bytes(raw)
            revisions.append(self.revision("file.txt"))
        self.assertEqual(len(set(revisions)), 3)

    def test_noop_preserves_mixed_newlines(self):
        raw = b"one\r\ntwo\n"
        path = self.write("mixed.txt", raw)
        result = self.replace("mixed.txt", "one", "one")
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(result.structured["state"], "unchanged")

    def test_concurrent_same_revision_has_only_one_success(self):
        self.write("file.txt", b"old\n")
        revision = self.revision("file.txt")
        barrier = threading.Barrier(2)
        def worker(text):
            barrier.wait(timeout=3)
            try:
                call_tool("write_file", self.ctx, {"path": "file.txt", "content": text, "expected_revision": revision})
                return "success"
            except ToolError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(worker, ("first\n", "second\n")))
        self.assertCountEqual(results, ["success", "REVISION_MISMATCH"])

    def test_atomic_write_retains_executable_permission_on_posix(self):
        if os.name == "nt":
            self.skipTest("POSIX 权限位")
        path = self.write("script.sh", b"old\n")
        path.chmod(0o755)
        atomic_write(path, "new\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)

    def test_subdirectory_listing_uses_workspace_relative_paths(self):
        self.write("child.txt", b"root file")
        self.write("sub/child.txt", b"child")
        result = call_tool("list_dir", self.ctx, {"path": "sub"}).structured
        self.assertEqual(result["files"], [{"path": "sub/child.txt", "size": 5}])

    def test_directory_link_outside_not_traversed_or_searched(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("OUTSIDE_MARKER", encoding="utf-8")
        link = self.workspace / "linked"
        if os.name == "nt":
            process = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
            if process.returncode:
                self.skipTest("当前系统无法创建测试 junction")
        else:
            link.symlink_to(outside, target_is_directory=True)
        self.assertFalse(any("linked" in rel for rel, _ in self.ws.iter_entries()))
        result = call_tool("search_text", self.ctx, {"pattern": "OUTSIDE_MARKER"})
        self.assertEqual(result.structured["count"], 0)
        with self.assertRaises(PathRejected):
            self.ws.resolve_existing("linked/secret.txt")

    def test_invalid_write_paths_rejected(self):
        for path in ("../outside", "C:/outside", "/tmp/outside", "bad\x00path"):
            with self.subTest(path=path), self.assertRaises(PathRejected):
                self.ws.resolve_for_write(path)
        if os.name == "nt":
            with self.assertRaises(PathRejected):
                self.ws.resolve_for_write("file.txt:stream")

    def test_internal_junction_is_skipped_without_python312_helper(self):
        if os.name != "nt":
            self.skipTest("Windows junction 兼容性")
        target = self.workspace / "real"
        target.mkdir()
        (target / "file.txt").write_bytes(b"inside")
        link = self.workspace / "alias"
        process = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if process.returncode:
            self.skipTest("当前系统无法创建测试 junction")
        with patch.object(type(link), "is_junction", return_value=False):
            self.assertFalse(self.ws._safe_entry(link))
            entries = list(self.ws.iter_entries())
        self.assertFalse(any("alias" in rel for rel, _ in entries))

    def patch_text(self):
        return "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-one\n+ONE\n--- a/two.txt\n+++ b/two.txt\n@@ -1 +1 @@\n-two\n+TWO\n"

    def test_crlf_patch_and_code_looking_like_file_headers(self):
        path = self.write("one.txt", b"one\r\n")
        diff = "--- a/one.txt\n+++ b/one.txt\n@@ -1 +1 @@\n-one\n+++ literal content\n"
        result = call_tool("apply_patch", self.ctx, {"patch": diff})
        self.assertEqual(path.read_bytes(), b"++ literal content\r\n")
        self.assertEqual(result.structured["changes"][0]["revision_after"], self.revision("one.txt"))

    def test_patch_new_file_deletion_and_missing_final_newline(self):
        diff = "--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+created\n\\ No newline at end of file\n"
        call_tool("apply_patch", self.ctx, {"patch": diff})
        self.assertEqual((self.workspace / "new.txt").read_bytes(), b"created")
        diff = "--- a/new.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-created\n\\ No newline at end of file\n"
        call_tool("apply_patch", self.ctx, {"patch": diff})
        self.assertFalse((self.workspace / "new.txt").exists())

    def test_patch_insertion_respects_declared_position(self):
        path = self.write("file.txt", b"one\ntwo\nthree\n")
        diff = "--- a/file.txt\n+++ b/file.txt\n@@ -2,0 +3 @@\n+insert\n"
        call_tool("apply_patch", self.ctx, {"patch": diff, "expected_revisions": {"file.txt": self.revision("file.txt")}})
        self.assertEqual(path.read_bytes(), b"one\ntwo\ninsert\nthree\n")

    def test_context_free_insertion_requires_revision(self):
        path = self.write("file.txt", b"one\ntwo\n")
        diff = "--- a/file.txt\n+++ b/file.txt\n@@ -1,0 +2 @@\n+insert\n"
        with self.assertRaises(ToolError) as caught:
            call_tool("apply_patch", self.ctx, {"patch": diff})
        self.assertEqual(caught.exception.code, "REVISION_REQUIRED")
        self.assertEqual(path.read_bytes(), b"one\ntwo\n")

    def test_corrupt_journal_does_not_prevent_server_start(self):
        directory = self.ws.editor.store / ("a" * 32)
        directory.mkdir(parents=True)
        (directory / "manifest.json").write_text("[]", encoding="utf-8")
        with self.server() as (server, _):
            self.assertEqual(server.recovery_report[0]["state"], "recovery_required")

    def test_workspace_lock_serializes_separate_processes(self):
        script = """
import sys, time
from pathlib import Path
from chatcode.workspace import Workspace
root, marker, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
with Workspace(root).write_lock:
    with marker.open('a') as f:
        f.write(label + ':start\\n'); f.flush()
    time.sleep(0.15)
    with marker.open('a') as f:
        f.write(label + ':end\\n'); f.flush()
"""
        marker = self.root / "lock-events.txt"
        processes = []
        try:
            for label in ("a", "b"):
                processes.append(subprocess.Popen([sys.executable, "-B", "-c", script, str(self.workspace), str(marker), label],
                                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
            for process in processes:
                out, err = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 0, err)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
        events = marker.read_text().splitlines()
        self.assertIn(events, (["a:start", "a:end", "b:start", "b:end"], ["b:start", "b:end", "a:start", "a:end"]))

    def test_patch_preflight_failure_changes_nothing(self):
        one = self.write("one.txt", b"one\n")
        two = self.write("two.txt", b"mismatch\n")
        with self.assertRaises(ToolError):
            call_tool("apply_patch", self.ctx, {"patch": self.patch_text()})
        self.assertEqual(one.read_bytes(), b"one\n")
        self.assertEqual(two.read_bytes(), b"mismatch\n")

    def test_patch_write_failure_rolls_back_byte_exactly(self):
        one = self.write("one.txt", b"one\r\n")
        two = self.write("two.txt", b"two\n")
        original_write = editing.atomic_write_bytes
        def fail(path, data, **kwargs):
            if path == two:
                raise OSError("injected write failure")
            return original_write(path, data, **kwargs)
        with patch.object(editing, "atomic_write_bytes", fail), self.assertRaises(ToolError) as caught:
            call_tool("apply_patch", self.ctx, {"patch": self.patch_text()})
        self.assertEqual(caught.exception.details["state"], "rolled_back")
        self.assertEqual(one.read_bytes(), b"one\r\n")
        self.assertEqual(two.read_bytes(), b"two\n")

    def test_rollback_does_not_overwrite_external_edit(self):
        one = self.write("one.txt", b"one\n")
        two = self.write("two.txt", b"two\n")
        original_write = editing.atomic_write_bytes
        def fail(path, data, **kwargs):
            if path == two:
                one.write_bytes(b"external edit\n")
                raise OSError("injected write failure")
            return original_write(path, data, **kwargs)
        with patch.object(editing, "atomic_write_bytes", fail), self.assertRaises(ToolError) as caught:
            call_tool("apply_patch", self.ctx, {"patch": self.patch_text()})
        self.assertEqual(caught.exception.details["state"], "recovery_required")
        self.assertEqual(one.read_bytes(), b"external edit\n")

    def test_interrupted_commit_is_recovered_on_next_server_start(self):
        one = self.write("one.txt", b"one\n")
        two = self.write("two.txt", b"two\n")
        original_write = editing.atomic_write_bytes
        def interrupt(path, data, **kwargs):
            if path == two:
                raise KeyboardInterrupt("simulated process interruption")
            return original_write(path, data, **kwargs)
        with patch.object(editing, "atomic_write_bytes", interrupt), self.assertRaises(KeyboardInterrupt):
            call_tool("apply_patch", self.ctx, {"patch": self.patch_text()})
        self.assertEqual(one.read_bytes(), b"ONE\n")
        with self.server() as (server, _):
            self.assertEqual(server.recovery_report[0]["state"], "rolled_back")
        self.assertEqual(one.read_bytes(), b"one\n")
        self.assertEqual(two.read_bytes(), b"two\n")

    def test_patch_dry_run_does_not_write_or_create_journal(self):
        self.write("one.txt", b"one\n")
        self.write("two.txt", b"two\n")
        result = call_tool("apply_patch", self.ctx, {"patch": self.patch_text(), "dry_run": True})
        self.assertTrue(result.structured["dry_run"])
        self.assertEqual((self.workspace / "one.txt").read_bytes(), b"one\n")
        self.assertFalse(self.ws.editor.store.exists())

    def test_duplicate_targets_are_rejected(self):
        self.write("one.txt", b"one\n")
        with self.assertRaises(FileError) as caught:
            self.ws.editor.commit([PreparedEdit("one.txt", b"one\n", b"ONE\n")] * 2)
        self.assertEqual(caught.exception.code, "DUPLICATE_PATH")

    def oauth_pair(self, server, method="none"):
        client = server.register_client({"redirect_uris": ["https://example.com/callback"], "token_endpoint_auth_method": method})
        verifier = "v" * 43
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        code = server.create_authorization_code(client_id=client["client_id"], redirect_uri=client["redirect_uris"][0],
                                                code_challenge=challenge, code_challenge_method="S256", resource="https://resource.example/mcp", scope="code")
        return client, verifier, code

    def test_oauth_enforces_client_auth_and_resource_binding(self):
        server = OAuthServer(password="test")
        client, verifier, code = self.oauth_pair(server, "client_secret_post")
        args = dict(code=code, client_id=client["client_id"], redirect_uri=client["redirect_uris"][0], code_verifier=verifier,
                    resource="https://resource.example/mcp")
        with self.assertRaises(OAuthError):
            server.exchange_code(**args)
        with self.assertRaises(OAuthError):
            server.exchange_code(**{**args, "resource": "https://other.example/mcp"}, client_secret=client["client_secret"])
        token = server.exchange_code(**args, client_secret=client["client_secret"])
        self.assertTrue(server.validate_token(token["access_token"], args["resource"]))
        self.assertFalse(server.validate_token(token["access_token"], "https://other.example/mcp"))
        with self.assertRaises(OAuthError):
            server.exchange_code(**args, client_secret=client["client_secret"])

    def test_refresh_is_bound_rotated_and_preserves_scope(self):
        server = OAuthServer(password="test")
        client, verifier, code = self.oauth_pair(server)
        token = server.exchange_code(code=code, client_id=client["client_id"], redirect_uri=client["redirect_uris"][0],
                                     code_verifier=verifier, resource="https://resource.example/mcp")
        other = server.register_client({"redirect_uris": ["https://example.com/other"]})
        with self.assertRaises(OAuthError):
            server.refresh_access_token(token["refresh_token"], "https://resource.example/mcp", client_id=other["client_id"])
        with self.assertRaises(OAuthError):
            server.refresh_access_token(token["refresh_token"], "https://other.example/mcp", client_id=client["client_id"])
        refreshed = server.refresh_access_token(token["refresh_token"], "https://resource.example/mcp", client_id=client["client_id"])
        self.assertEqual(refreshed["scope"], "code")
        self.assertNotEqual(refreshed["refresh_token"], token["refresh_token"])
        with self.assertRaises(OAuthError):
            server.refresh_access_token(token["refresh_token"], "https://resource.example/mcp", client_id=client["client_id"])

    def test_oauth_rejects_unsafe_redirects_and_plain_pkce(self):
        server = OAuthServer(password="test")
        for uri in ("javascript:alert(1)", "http://example.com/cb", "https://user:pass@example.com/cb", "https://example.com/cb#fragment"):
            with self.subTest(uri=uri), self.assertRaises(OAuthError):
                server.register_client({"redirect_uris": [uri]})
        client = server.register_client({"redirect_uris": ["http://[::1]/cb"]})
        with self.assertRaises(OAuthError):
            server.create_authorization_code(client_id=client["client_id"], redirect_uri="http://[::1]/cb",
                                             code_challenge="x" * 43, code_challenge_method="plain", resource="http://127.0.0.1/mcp")

    def test_oauth_http_authorization_basic_exchange_and_refresh(self):
        with self.server(mode="oauth") as (_, base):
            headers = {"Content-Type": "application/json"}
            payload = {"redirect_uris": ["https://example.com/callback"], "token_endpoint_auth_method": "client_secret_basic"}
            status, body, _ = self.http(base + "/oauth/register", json.dumps(payload).encode(), headers=headers)
            self.assertEqual(status, 201)
            client = json.loads(body)
            verifier = "v" * 43
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            form = {"client_id": client["client_id"], "redirect_uri": "https://example.com/callback", "password": self.settings.oauth_password,
                    "code_challenge": challenge, "code_challenge_method": "S256", "resource": base + "/mcp", "state": "state123"}
            form_headers = {"Content-Type": "application/x-www-form-urlencoded"}
            status, _, response_headers = self.http(base + "/oauth/authorize", urllib.parse.urlencode(form).encode(), headers=form_headers)
            self.assertEqual(status, 302)
            query = urllib.parse.parse_qs(urllib.parse.urlparse(response_headers["Location"]).query)
            self.assertEqual(query["state"], ["state123"])
            token_form = {"grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": form["redirect_uri"], "code_verifier": verifier}
            token_headers = {**form_headers, "Authorization": "Basic " + base64.b64encode((client["client_id"] + ":" + client["client_secret"]).encode()).decode()}
            status, body, _ = self.http(base + "/oauth/token", urllib.parse.urlencode(token_form).encode(), headers=token_headers)
            self.assertEqual(status, 200, body)
            token = json.loads(body)
            status, body, _ = self.http(base + "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode(),
                                      headers={"Content-Type": "application/json", "Authorization": "Bearer " + token["access_token"]})
            self.assertEqual(status, 200)
            self.assertIn("result", json.loads(body))
            refresh = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]}
            status, body, _ = self.http(base + "/oauth/token", urllib.parse.urlencode(refresh).encode(), headers=token_headers)
            self.assertEqual(status, 200, body)
            self.assertNotEqual(json.loads(body)["refresh_token"], token["refresh_token"])

    def test_public_refresh_without_client_id_is_supported_but_confidential_requires_auth(self):
        with self.server(mode="oauth") as (http_server, base):
            oauth = http_server.oauth
            for method in ("none", "client_secret_post"):
                client, verifier, code = self.oauth_pair(oauth, method)
                token = oauth.exchange_code(code=code, client_id=client["client_id"], redirect_uri=client["redirect_uris"][0],
                                            code_verifier=verifier, resource="https://resource.example/mcp", client_secret=client.get("client_secret"))
                # 模拟该令牌对应的公网 origin；刷新请求仍发送到本地测试服务。
                self.settings.public_url = "https://resource.example"
                form = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]}
                status, body, _ = self.http(base + "/oauth/token", urllib.parse.urlencode(form).encode(),
                                           headers={"Content-Type": "application/x-www-form-urlencoded"})
                self.assertEqual(status, 200 if method == "none" else 401, body)

    def test_runtime_restarts_tunnel_without_treating_discovered_url_as_fixed(self):
        class FakeTunnel:
            def __init__(self, url):
                self.public_url, self.process, self.running = url, object(), True
            def alive(self):
                return self.running
            def stop(self):
                self.running = False
        first, second = FakeTunnel("https://first.example"), FakeTunnel("https://second.example")
        second_ready = threading.Event()
        calls = []
        def start(settings, log):
            calls.append(settings.public_url)
            return first if len(calls) == 1 else second
        self.settings.tunnel = "cloudflared"
        runtime = ServiceRuntime(self.settings, log=lambda _: None,
                                 on_ready=lambda url: second_ready.set() if url == second.public_url else None, retry_delay=0.01)
        with patch("chatcode.runtime.start_tunnel", start):
            try:
                runtime.start()
                self.assertTrue(runtime.ready.wait(timeout=3))
                first.running = False
                self.assertTrue(second_ready.wait(timeout=3))
                self.assertEqual(calls, ["", ""])
                self.assertEqual(self.settings.discovered_public_url, second.public_url)
                self.assertTrue(self.settings.mcp_url().startswith(second.public_url))
            finally:
                runtime.stop()
        self.assertFalse(second.running)
        self.assertFalse(runtime._monitor_thread.is_alive())
        self.assertFalse(runtime._http_thread.is_alive())

    def test_cli_and_gui_share_path_default(self):
        self.assertEqual(build_arg_parser().parse_args([]).auth_mode, "path")
        self.assertEqual(Settings(workspace=self.workspace).auth_mode, "path")


if __name__ == "__main__":
    unittest.main()
