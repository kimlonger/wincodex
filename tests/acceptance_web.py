"""显式浏览器验收页，仅观察本脚本自建窗口；300 秒后关闭全部测试资源。"""

import argparse
import base64
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatcode.config import Settings
from chatcode.mcp_http import create_server

HTML = r'''<!doctype html><meta charset="utf-8"><title>ChatCode MCP 浏览器图像验收</title>
<style>body{font:17px system-ui;max-width:960px;margin:40px auto;background:#f5f6fa;color:#17213a}button{padding:12px 22px;background:#244fd9;color:white;border:0;border-radius:8px;font-size:17px}img{display:block;margin-top:25px;max-width:100%;border:1px solid #ccc}pre{white-space:pre-wrap}</style>
<h1>ChatCode MCP 浏览器图像验收</h1><p>仅观察本机临时测试窗口。此页验证 MCP 协议返回的 PNG 在真实浏览器中解码和显示。</p>
<button id="observe">读取测试窗口</button><pre id="status">等待读取</pre><img id="image" alt="等待 MCP 窗口截图">
<script>
let counter=0,session=''; const status=document.querySelector('#status');
async function rpc(method,params){let r=await fetch(location.pathname+'/rpc',{method:'POST',headers:{'Content-Type':'application/json',...(session?{'Mcp-Session-Id':session}:{})},body:JSON.stringify({jsonrpc:'2.0',id:++counter,method,params})});session=r.headers.get('Mcp-Session-Id')||session;let v=await r.json();if(v.error)throw Error(JSON.stringify(v.error));return v.result;}
document.querySelector('#observe').onclick=async()=>{try{
status.textContent='连接 MCP…';let init=await rpc('initialize',{protocolVersion:'2025-06-18',capabilities:{},clientInfo:{name:'ChatCode-browser-acceptance',version:'1'}});
let tools=await rpc('tools/list',{});if(!tools.tools.some(t=>t.name==='desktop_observe'))throw Error('缺少图像工具');
let value=await rpc('tools/call',{name:'desktop_observe',arguments:{image:true}});if(value.isError)throw Error(JSON.stringify(value));
let picture=value.content.find(c=>c.type==='image'&&c.mimeType==='image/png');if(!picture)throw Error('MCP 未返回 PNG 内容块');
let image=document.querySelector('#image');image.onload=async()=>{let report={initialize:true,tools_list:true,mcp_image_content:true,browser_png_decoded:image.naturalWidth>0,width:image.naturalWidth,height:image.naturalHeight,observed_window_foreground:value.structuredContent?.observation?.foreground,server_version:init.serverInfo.version,client:'local_browser_acceptance_page',chatgpt_client_tested:false};await fetch(location.pathname+'/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(report)});status.textContent='验收通过：MCP 初始化 → 工具发现 → 窗口观察 → PNG 浏览器显示\n'+JSON.stringify(report,null,2)};image.src='data:image/png;base64,'+picture.data;
}catch(e){status.textContent='验收失败：'+e.message;}};
</script>'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--background", action="store_true", help="另建测试窗口遮挡目标，验收后台只读观察")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="chatcode-web-acceptance-") as directory, ExitStack() as resources:
        base = Path(directory)
        fixture_ready = base / "fixture.json"
        fixture = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("desktop_fixture.py")), str(fixture_ready)], creationflags=subprocess.CREATE_NO_WINDOW)
        resources.callback(lambda: (fixture.terminate(), fixture.wait(5)))
        server, web, cover = None, None, None
        try:
            deadline = time.monotonic() + 10
            while not fixture_ready.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            handle = json.loads(fixture_ready.read_text())["handle"]
            if args.background:
                cover_ready = base / "cover.json"
                cover = subprocess.Popen([sys.executable, "-B", str(Path(__file__).with_name("desktop_fixture.py")),
                                          str(cover_ready), "ChatCode Background Observation Cover"],
                                         creationflags=subprocess.CREATE_NO_WINDOW)
                resources.callback(lambda: (cover.terminate(), cover.wait(5)))
                deadline = time.monotonic() + 10
                while not cover_ready.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                assert cover_ready.exists(), "Cover fixture did not start"
            with patch.dict(os.environ, CHATCODE_DATA_DIR=str(base / "data")):
                settings = Settings(workspace=base, port=0, auth_mode="none", tunnel="none", read_only=True, allow_exec=False,
                                    desktop_python=args.python, desktop_window=handle)
                server = create_server(settings)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                resources.callback(lambda: (server.shutdown(), server.server_close()))
                endpoint = f"http://127.0.0.1:{server.server_address[1]}/mcp"
                prefix = "/" + secrets.token_hex(16)
                session_id = ""
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass
                    def reply(self, code, data, content_type="application/json", headers=None):
                        self.send_response(code)
                        self.send_header("Content-Type", content_type)
                        self.send_header("Content-Length", str(len(data)))
                        self.send_header("Cache-Control", "no-store")
                        for key, value in (headers or {}).items():
                            self.send_header(key, value)
                        self.end_headers()
                        self.wfile.write(data)
                    def do_GET(self):
                        if self.path == prefix:
                            self.reply(200, HTML.encode(), "text/html; charset=utf-8")
                        else:
                            self.reply(404, b'{}')
                    def do_POST(self):
                        nonlocal session_id
                        try:
                            count = int(self.headers.get("Content-Length", "0"))
                            if not 0 < count <= 16000:
                                raise ValueError("size")
                            raw = self.rfile.read(count)
                            value = json.loads(raw)
                            if self.path == prefix + "/report":
                                args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
                                self.reply(200, b'{}')
                                return
                            if self.path != prefix + "/rpc" or value.get("method") not in ("initialize", "tools/list", "tools/call"):
                                raise ValueError("method")
                            if value["method"] == "tools/call" and value.get("params", {}).get("name") not in ("desktop_status", "desktop_observe"):
                                raise ValueError("tool")
                            headers = {"Content-Type": "application/json"}
                            if session_id:
                                headers["Mcp-Session-Id"] = session_id
                            with opener.open(urllib.request.Request(endpoint, data=raw, headers=headers), timeout=25) as response:
                                data = response.read()
                                session_id = response.headers.get("Mcp-Session-Id") or session_id
                                self.reply(200, data, headers={"Mcp-Session-Id": session_id} if session_id else {})
                        except Exception as exc:
                            self.reply(400, json.dumps({"error": {"message": str(exc)}}).encode())
                web = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                threading.Thread(target=web.serve_forever, daemon=True).start()
                resources.callback(lambda: (web.shutdown(), web.server_close()))
                args.ready.write_text(json.dumps({"url": f"http://127.0.0.1:{web.server_address[1]}{prefix}", "handle": handle}), encoding="utf-8")
                print("Browser acceptance ready", flush=True)
                deadline = time.monotonic() + 300
                while time.monotonic() < deadline and not Path(str(args.ready) + ".stop").exists():
                    time.sleep(.3)
        finally:
            resources.close()


if __name__ == "__main__":
    main()
