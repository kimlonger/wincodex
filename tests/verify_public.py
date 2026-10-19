"""公网可达性验证：真起服务、真开隧道、真从公网发请求。

验证链路：公网 HTTPS → Cloudflare 边缘 → cloudflared → 本地 MCP 服务
"""

from __future__ import annotations

import json
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chatcode.config import Settings  # noqa: E402
from chatcode.mcp_http import create_server  # noqa: E402
from chatcode.tunnel import start_tunnel  # noqa: E402

PASS, FAIL = "✅", "❌"


# 关键：必须绕开本机代理发请求。urllib 默认会读 HTTP_PROXY 环境变量，
# 那样测的是"代理能不能访问"，而我们要测的是"ChatGPT 从云端直连能不能访问"。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_OPENER)


def http(url: str, method: str = "GET", body: bytes | None = None, headers: dict | None = None, timeout: int = 25, retries: int = 4):
    """发请求；隧道刚建立时边缘节点可能需要几秒才生效，所以带重试。"""
    last = (0, b"")
    for attempt in range(retries):
        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except Exception as exc:
            last = (0, str(exc).encode())
            if attempt < retries - 1:
                time.sleep(3)
    return last


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def main() -> int:
    sandbox = Path(tempfile.mkdtemp(prefix="chatcode-verify-"))
    (sandbox / "demo.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")

    port = free_port()
    settings = Settings(workspace=sandbox, port=port, auth_mode="oauth", tunnel="cloudflared")
    server = create_server(settings)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.3}, daemon=True).start()
    print(f"本地服务：http://127.0.0.1:{port}/mcp")

    print("正在打通隧道…")
    tunnel = start_tunnel(settings, log=lambda m: print(f"  {m}"))
    if tunnel is None:
        print(f"{FAIL} 隧道启动失败")
        return 1
    public = tunnel.wait_for_url(timeout=60)
    if not public:
        print(f"{FAIL} 未取得公网地址")
        return 1

    # Cloudflare 明说"it may take some time to be reachable"：实测首次可达约需 30-40 秒，
    # 提前请求会拿到 502。这里先等，再开始验证。
    print("等待隧道在边缘节点生效（约 35 秒）…")
    time.sleep(35)

    print(f"\n公网地址：{public}\n")
    checks = []

    print("[1] 健康检查（公网 → 本地）")
    status, body = http(public + "/health")
    ok = status == 200 and b"ok" in body
    checks.append(("公网 /health 可达", ok))
    print(f"  {PASS if ok else FAIL} HTTP {status}  {body[:90].decode('utf-8', 'replace')}")

    print("\n[2] OAuth 发现端点（ChatGPT 会先来这里）")
    status, body = http(public + "/.well-known/oauth-authorization-server")
    ok = status == 200
    if ok:
        try:
            meta = json.loads(body.decode("utf-8"))
            ok = "authorization_endpoint" in meta and meta["issuer"].startswith("https://")
            print(f"     issuer={meta.get('issuer')}")
            print(f"     token={meta.get('token_endpoint')}")
        except Exception:
            ok = False
    checks.append(("OAuth 元数据正确", ok))
    print(f"  {PASS if ok else FAIL} HTTP {status}")

    status, body = http(public + "/.well-known/oauth-protected-resource")
    ok = status == 200
    checks.append(("受保护资源元数据", ok))
    print(f"  {PASS if ok else FAIL} HTTP {status}")

    print("\n[3] 未认证访问必须被拒")
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode()
    status, body = http(public + "/mcp", "POST", payload, {"Content-Type": "application/json"})
    ok = status == 401
    checks.append(("无令牌返回 401", ok))
    print(f"  {PASS if ok else FAIL} HTTP {status}")

    print("\n[4] 动态客户端注册（RFC 7591，ChatGPT 会自动做）")
    reg = json.dumps({"client_name": "verify", "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"], "token_endpoint_auth_method": "none"}).encode()
    status, body = http(public + "/oauth/register", "POST", reg, {"Content-Type": "application/json"})
    client_id = ""
    if status in (200, 201):
        try:
            client_id = json.loads(body.decode("utf-8")).get("client_id", "")
        except Exception:
            pass
    ok = bool(client_id)
    checks.append(("动态注册成功", ok))
    print(f"  {PASS if ok else FAIL} HTTP {status}  client_id={client_id[:18]}…")

    print("\n[5] MCP 端点不提供 SSE（Quick Tunnel 不支持）")
    status, body = http(public + "/mcp", "GET")
    ok = status == 405
    checks.append(("GET /mcp 返回 405", ok))
    print(f"  {PASS if ok else FAIL} HTTP {status}")

    print()
    tunnel.stop()
    server.shutdown()
    server.server_close()
    shutil.rmtree(sandbox, ignore_errors=True)

    failed = [n for n, ok, in checks if not ok]
    print("=" * 56)
    print(f"公网链路验证：{len(checks) - len(failed)}/{len(checks)} 通过")
    if failed:
        for name in failed:
            print(f"  {FAIL} {name}")
        return 1
    print("公网全链路打通 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
