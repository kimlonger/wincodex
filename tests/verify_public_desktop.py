"""临时只读样例的公网 MCP/图像/切换项目验收；不暴露用户工作区。"""

import argparse
import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatcode.config import Settings
from chatcode.mcp_http import create_server
from chatcode.tunnel import start_tunnel


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = {"passed": False, "client": "https_protocol_probe", "chatgpt_client_tested": False}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    fixture = server = tunnel = None
    try:
        with tempfile.TemporaryDirectory(prefix="chatcode-public-desktop-") as directory, ExitStack() as resources:
            base = Path(directory)
            first, second = base / "first", base / "second"
            first.mkdir()
            second.mkdir()
            (first / "marker.txt").write_text("TEMPORARY_FIRST_PROJECT", encoding="utf-8")
            (second / "marker.txt").write_text("TEMPORARY_SECOND_PROJECT", encoding="utf-8")
            ready = base / "fixture.json"
            fixture = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("desktop_fixture.py")), str(ready)],
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            resources.callback(lambda: (fixture.terminate(), fixture.wait(5)))
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            handle = json.loads(ready.read_text())["handle"]
            with patch.dict(os.environ, CHATCODE_DATA_DIR=str(base / "state")):
                settings = Settings(workspace=first, port=0, auth_mode="path", read_only=True, allow_exec=False,
                                    desktop_python=args.python, desktop_window=handle, tunnel="cloudflared")
                server = create_server(settings)
                settings.port = server.server_address[1]
                threading.Thread(target=server.serve_forever, daemon=True).start()
                resources.callback(lambda: (server.shutdown(), server.server_close()))
                print("Starting temporary read-only HTTPS tunnel", flush=True)
                tunnel = start_tunnel(settings, log=lambda _message: None)
                assert tunnel is not None, "Tunnel failed to start"
                resources.callback(tunnel.stop)
                public = tunnel.wait_for_url(50)
                assert public.startswith("https://"), "No HTTPS tunnel address"
                settings.discovered_public_url = public
                endpoint = settings.mcp_url()
                session = ""
                def http(url, payload=None):
                    headers = {"Content-Type": "application/json"}
                    if session:
                        headers["Mcp-Session-Id"] = session
                    raw = None if payload is None else json.dumps(payload).encode()
                    with opener.open(urllib.request.Request(url, data=raw, headers=headers), timeout=30 if payload else 8) as response:
                        return response.status, response.read(), response.headers.get("Mcp-Session-Id", "")
                deadline = time.monotonic() + 80
                while True:
                    try:
                        assert http(public + "/health")[0] == 200
                        break
                    except Exception:
                        if time.monotonic() >= deadline:
                            raise AssertionError("HTTPS health endpoint not reachable") from None
                        time.sleep(2)
                result["public_https_health"] = True
                print("HTTPS health passed; verifying authentication and MCP", flush=True)
                try:
                    http(public + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                    raise AssertionError("Missing path credential unexpectedly accepted")
                except urllib.error.HTTPError as exc:
                    assert exc.code == 404, exc.code
                result["missing_credential_rejected"] = True
                def rpc(method, params):
                    nonlocal session
                    status, raw, assigned = http(endpoint, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                    assert status == 200
                    session = assigned or session
                    value = json.loads(raw)
                    assert "error" not in value, value.get("error")
                    return value["result"]
                initialized = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                 "clientInfo": {"name": "public-readonly-acceptance", "version": "1"}})
                result["server_version"] = initialized["serverInfo"]["version"]
                tools = rpc("tools/list", {})["tools"]
                assert any(tool["name"] == "desktop_observe" for tool in tools)
                result["initialize_and_tools_list"] = True
                for attempt in range(5):
                    observed = rpc("tools/call", {"name": "desktop_observe", "arguments": {"image": True}})
                    if not observed.get("isError") or observed.get("structuredContent", {}).get("code") != "USER_TAKEOVER":
                        break
                    time.sleep(.4)
                assert not observed.get("isError"), observed.get("structuredContent", {}).get("code", "desktop_observe failed")
                result["readonly_observation_retries"] = attempt
                picture = next(item for item in observed["content"] if item["type"] == "image")
                assert picture["mimeType"] == "image/png"
                raw = base64.b64decode(picture["data"], validate=True)
                assert raw.startswith(b"\x89PNG\r\n\x1a\n")
                size = struct.unpack(">II", raw[16:24])
                assert size == (600, 330), size
                args.output.with_suffix(".png").write_bytes(raw)
                result.update(public_mcp_png=True, image_size=list(size))
                def project_contexts():
                    return rpc("tools/call", {"name": "list_workspaces", "arguments": {}})["structuredContent"]["workspaces"]
                def read_marker(project):
                    arguments = {"path": "marker.txt", **{key: project[key] for key in ("workspace_id", "workspace_token")}}
                    value = rpc("tools/call", {"name": "read_file", "arguments": arguments})
                    assert not value.get("isError"), value
                    return json.dumps(value)
                first_context = project_contexts()[0]
                assert "TEMPORARY_FIRST_PROJECT" in read_marker(first_context)
                server.switch_workspace(second)
                assert settings.mcp_url() == endpoint
                unscoped = rpc("tools/call", {"name": "read_file", "arguments": {"path": "marker.txt"}})
                assert unscoped.get("structuredContent", {}).get("code") == "WORKSPACE_REQUIRED"
                second_context = next(project for project in project_contexts() if project["active"])
                assert "TEMPORARY_SECOND_PROJECT" in read_marker(second_context)
                assert "TEMPORARY_FIRST_PROJECT" in read_marker(first_context)
                result.update(switch_project_same_public_url=True, explicit_project_context_enforced=True,
                              previous_project_context_still_valid=True, only_temporary_readonly_project=True, passed=True)
                print("Public MCP PNG and project switching passed", flush=True)
    except Exception as exc:
        # 仅报告异常类型；不把端点路径中的临时凭据写进报告。
        result["failure_type"] = type(exc).__name__
        if isinstance(exc, AssertionError):
            result["failure_reason"] = str(exc)[:100]
        print("Public acceptance failed: " + type(exc).__name__, flush=True)
    finally:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
