"""端到端冒烟测试：真实起服务器、真实走 HTTP、真实读写文件。

用法：python tests/smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatcode.config import Settings  # noqa: E402
from chatcode.mcp_http import create_server  # noqa: E402

PASS = "✅"
FAIL = "❌"
results: list[tuple[str, bool, str]] = []
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f"  — {detail}" if detail else ""))


def rpc(url: str, method: str, params: dict, rid: int = 1, token: str | None = None) -> dict:
    payload = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"http_error": exc.code, "body": exc.read().decode("utf-8", errors="replace")}


def _run() -> int:
    # 造一个临时工作区
    sandbox = Path(tempfile.mkdtemp(prefix="chatcode-smoke-"))
    (sandbox / "src").mkdir()
    (sandbox / "src" / "main.py").write_text(
        "def hello():\n    return 'hello world'\n\n\ndef add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    (sandbox / "README.md").write_text("# demo\n\n这是一个测试项目。\n", encoding="utf-8")
    print(f"测试工作区：{sandbox}\n")

    settings = Settings(workspace=sandbox, host="127.0.0.1", port=0, auth_mode="none", allow_exec=True)
    # 先占到一个空闲端口再交给服务器，避免端口冲突
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    settings.port = port
    server = create_server(settings)

    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    worker.start()
    time.sleep(0.6)
    url = f"http://127.0.0.1:{port}/mcp"
    print(f"服务器地址：{url}\n")

    try:
        print("[1] 协议握手")
        resp = rpc(url, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}})
        result = resp.get("result", {})
        check("initialize 返回协议版本", result.get("protocolVersion") == "2025-06-18", str(result.get("protocolVersion")))
        check("serverInfo 正确", result.get("serverInfo", {}).get("name") == "chatcode-mcp")
        check("instructions 已下发", bool(result.get("instructions")))

        print("\n[2] 工具清单")
        resp = rpc(url, "tools/list", {}, rid=2)
        tools = resp.get("result", {}).get("tools", [])
        names = [t["name"] for t in tools]
        check("工具数量 >= 10", len(tools) >= 10, f"{len(tools)} 个")
        for expected in ("read_file", "read_files", "write_file", "replace_in_file", "search_text", "exec_command", "apply_patch"):
            check(f"包含 {expected}", expected in names)

        print("\n[3] 查文件")
        resp = rpc(url, "tools/call", {"name": "list_dir", "arguments": {"path": ".", "recursive": True}}, rid=3)
        struct = resp.get("result", {}).get("structuredContent", {})
        check("list_dir 列出文件", struct.get("count", 0) >= 2, f"count={struct.get('count')}")

        resp = rpc(url, "tools/call", {"name": "read_file", "arguments": {"path": "src/main.py"}}, rid=4)
        struct = resp.get("result", {}).get("structuredContent", {})
        check("read_file 读到内容", "hello world" in struct.get("content", ""))
        check("read_file 返回 revision", bool(struct.get("revision")))
        revision = struct.get("revision", "")

        resp = rpc(url, "tools/call", {"name": "read_files", "arguments": {"paths": ["src/main.py", "README.md"]}}, rid=5)
        struct = resp.get("result", {}).get("structuredContent", {})
        check("read_files 批量读到 2 个", struct.get("count") == 2 and all(f.get("ok") for f in struct.get("files", [])))

        resp = rpc(url, "tools/call", {"name": "search_text", "arguments": {"pattern": "def ", "path": "."}}, rid=6)
        struct = resp.get("result", {}).get("structuredContent", {})
        check("search_text 找到匹配", struct.get("count", 0) >= 2, f"{struct.get('count')} 处")

        print("\n[4] 改文件")
        resp = rpc(
            url,
            "tools/call",
            {
                "name": "replace_in_file",
                "arguments": {
                    "path": "src/main.py",
                    "old_text": "return 'hello world'",
                    "new_text": "return 'hello chatcode'",
                    "expected_revision": revision,
                },
            },
            rid=7,
        )
        struct = resp.get("result", {}).get("structuredContent", {})
        check("replace_in_file 替换成功", struct.get("replacements") == 1, str(struct.get("code", "")))
        check("磁盘内容真的变了", "hello chatcode" in (sandbox / "src" / "main.py").read_text(encoding="utf-8"))

        # 过期 revision 必须被拒绝
        resp = rpc(
            url,
            "tools/call",
            {
                "name": "replace_in_file",
                "arguments": {"path": "src/main.py", "old_text": "def add", "new_text": "def plus", "expected_revision": revision},
            },
            rid=8,
        )
        is_err = resp.get("result", {}).get("isError")
        struct = resp.get("result", {}).get("structuredContent", {})
        check("过期 revision 被拒绝", bool(is_err) and struct.get("code") == "REVISION_MISMATCH", str(struct.get("code")))

        # 写新文件
        resp = rpc(
            url,
            "tools/call",
            {"name": "write_file", "arguments": {"path": "src/new.py", "content": "print('new file')\n"}},
            rid=9,
        )
        struct = resp.get("result", {}).get("structuredContent", {})
        check("write_file 新建文件", (sandbox / "src" / "new.py").exists(), str(struct.get("revision", "")))

        print("\n[5] 补丁")
        patch = (
            "--- a/src/new.py\n"
            "+++ b/src/new.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-print('new file')\n"
            "+print('patched file')\n"
        )
        resp = rpc(url, "tools/call", {"name": "apply_patch", "arguments": {"patch": patch}}, rid=10)
        struct = resp.get("result", {}).get("structuredContent", {})
        check("apply_patch 应用成功", struct.get("count") == 1, str(struct.get("code", "")))
        check("补丁内容已落盘", "patched file" in (sandbox / "src" / "new.py").read_text(encoding="utf-8"))

        print("\n[6] 命令执行")
        resp = rpc(
            url,
            "tools/call",
            {"name": "exec_command", "arguments": {"command": "python -c \"print(6*7)\"", "timeout": 30, "yield_time_ms": 10_000}},
            rid=11,
        )
        struct = resp.get("result", {}).get("structuredContent", {})
        check("exec_command 执行成功", struct.get("exit_code") == 0, f"退出码 {struct.get('exit_code')}")
        check("命令输出正确", "42" in struct.get("stdout", ""), struct.get("stdout", "").strip()[:40])

        print("\n[7] 安全边界")
        for bad_path, label in (
            ("../outside.txt", "父目录穿越"),
            ("C:/Windows/win.ini", "盘符绝对路径"),
            ("/etc/passwd", "Unix 绝对路径"),
        ):
            resp = rpc(url, "tools/call", {"name": "read_file", "arguments": {"path": bad_path}}, rid=12)
            is_err = resp.get("result", {}).get("isError")
            check(f"拒绝{label}", bool(is_err))

        resp = rpc(url, "tools/call", {"name": "exec_command", "arguments": {"command": "rm -rf /"}}, rid=13)
        check("拒绝破坏性命令", bool(resp.get("result", {}).get("isError")))

        print("\n[8] 认证")
        settings.auth_mode = "bearer"
        resp = rpc(url, "tools/list", {}, rid=14)
        check("无令牌时拒绝访问", "http_error" in resp and resp["http_error"] == 401, str(resp.get("http_error")))
        resp = rpc(url, "tools/list", {}, rid=15, token=settings.bearer_token)
        check("带令牌时放行", "result" in resp)

        settings.auth_mode = "none"

        print("\n[9] 工具注解（决定客户端弹不弹确认框）")
        resp = rpc(url, "tools/list", {}, rid=16)
        by_name = {t["name"]: t for t in resp.get("result", {}).get("tools", [])}
        check("read_file 标记只读", by_name.get("read_file", {}).get("annotations", {}).get("readOnlyHint") is True)
        check("search_text 标记只读", by_name.get("search_text", {}).get("annotations", {}).get("readOnlyHint") is True)
        check("write_file 未标只读", by_name.get("write_file", {}).get("annotations", {}).get("readOnlyHint") is False)
        check("exec_command 标记破坏性", by_name.get("exec_command", {}).get("annotations", {}).get("destructiveHint") is True)
        check("所有工具都有注解", all("annotations" in t for t in by_name.values()))

        print("\n[10] 刷新令牌（避免反复授权）")
        import base64
        import hashlib

        registered = server.oauth.register_client(
            {"client_name": "smoke", "redirect_uris": ["https://example.com/cb"], "token_endpoint_auth_method": "none"}
        )
        verifier = "a" * 43
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        code = server.oauth.create_authorization_code(
            client_id=registered["client_id"],
            redirect_uri="https://example.com/cb",
            code_challenge=challenge,
            code_challenge_method="S256",
            resource="http://127.0.0.1/mcp",
        )
        first = server.oauth.exchange_code(
            code=code,
            client_id=registered["client_id"],
            redirect_uri="https://example.com/cb",
            code_verifier=verifier,
            resource="http://127.0.0.1/mcp",
        )
        check("签发 refresh_token", bool(first.get("refresh_token")))
        check("access_token 可用", server.oauth.validate_token(first["access_token"]))
        second = server.oauth.refresh_access_token(first["refresh_token"], "http://127.0.0.1/mcp")
        check("刷新拿到新 access_token", bool(second.get("access_token")))
        check("新 access_token 可用", server.oauth.validate_token(second["access_token"]))
        check("刷新令牌已轮换", second.get("refresh_token") != first.get("refresh_token"))

        print("\n[11] path 模式（凭据藏在 URL 里，无需授权页）")
        settings.auth_mode = "path"
        base = f"http://127.0.0.1:{port}"
        bare = rpc(base + "/mcp", "tools/list", {}, rid=17)
        check("不带令牌的 /mcp 被拒", bare.get("http_error") == 404, str(bare.get("http_error")))
        guarded = rpc(base + settings.mcp_path(), "tools/list", {}, rid=18)
        check("带令牌的路径可用", "result" in guarded, str(guarded.get("http_error", "")))
        wrong = rpc(base + "/mcp/not-the-token", "tools/list", {}, rid=19)
        check("错误令牌被拒", wrong.get("http_error") == 404, str(wrong.get("http_error")))
        with _OPENER.open(base + "/health", timeout=5) as response:
            health = json.load(response)
        check("path 模式不泄露工作区", health.get("workspace") == "hidden" and str(sandbox) not in json.dumps(health))
        settings.auth_mode = "none"
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(sandbox, ignore_errors=True)

    print("\n" + "=" * 60)
    failed = [name for name, ok, _ in results if not ok]
    print(f"总计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    if failed:
        print("失败项：")
        for name in failed:
            print(f"  - {name}")
        return 1
    print("全部通过 ✅")
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    results.clear()
    with tempfile.TemporaryDirectory(prefix="chatcode-smoke-state-") as state:
        with patch.dict(os.environ, {"CHATCODE_DATA_DIR": state}):
            return _run()


if __name__ == "__main__":
    raise SystemExit(main())
