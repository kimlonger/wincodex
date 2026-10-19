"""命令行入口：起 MCP 服务 → 起隧道 → 打印可直接粘贴到 ChatGPT 的地址。"""

from __future__ import annotations

import argparse
import time

from .config import APP_TITLE, MCP_ENDPOINT_PATH, Settings, settings_from_args
from .runtime import ServiceRuntime


BANNER = r"""
   ___  _  _   _  ___ ___  ___  ___   __  __ ___ ___
  / __|| || | / ||_ _/ __|/ __|/ _ \ |  \/  | _ \ _ \
 | (__ | __ || || | |\__ \ (_ | (_) || |\/| |  _/  _/
  \___||_||_||_||___|___/\___|\___/ |_|  |_|_| |_|
"""


def _print_ready(settings: Settings, public_url: str, log) -> None:
    origin = public_url.rstrip("/") if public_url else f"http://{settings.host}:{settings.port}"
    mcp_url = settings.mcp_url()
    print()
    print("=" * 68)
    print("  服务已就绪")
    print("=" * 68)
    print(f"  工作区      : {settings.workspace}")
    print(f"  MCP 地址    : {mcp_url}")
    print(f"  本地监听    : http://{settings.host}:{settings.port}{MCP_ENDPOINT_PATH}")
    print(f"  认证模式    : {settings.auth_mode}")
    print(f"  工具权限    : {'只读' if settings.read_only else '允许编辑'}；命令{'开启' if settings.allow_exec else '关闭'}")
    print(f"  敏感文件    : {'允许访问' if settings.allow_sensitive_files else '默认屏蔽 .env / 私钥'}（不属于命令沙箱）")
    print()
    if settings.auth_mode == "oauth":
        print(f"  授权口令    : {settings.oauth_password}")
        print()
        print("  接入步骤（ChatGPT 网页版）：")
        print("    1. 设置 → 开启开发者模式（Developer Mode）")
        print("    2. 新建一个自定义连接器 / MCP 应用")
        print(f"    3. 服务器 URL 填：{mcp_url}")
        print("    4. 认证方式选 OAuth，授权页输入上面的口令")
        print("    5. 新建对话，在工具菜单里启用它，然后说你的需求")
    elif settings.auth_mode == "path":
        print()
        print("  接入步骤（无需任何授权动作）：")
        print("    1. 设置 → 开启开发者模式（Developer Mode）")
        print("    2. 新建一个自定义连接器 / MCP 应用")
        print(f"    3. 服务器 URL 填：{mcp_url}")
        print("    4. 认证方式选「无认证」(No Auth)，直接保存")
        print("    5. 新建对话，在工具菜单里启用它，然后说你的需求")
        print()
        print("  说明：地址末尾那串随机字符就是凭据，不用口令也不会弹授权页，")
        print("        但别把完整地址发到公开场合。")
    elif settings.auth_mode == "bearer":
        print(f"  Bearer 令牌 : {settings.bearer_token}")
        print()
        print("  客户端配置（Claude Desktop / Cursor / Cline 等）：")
        print('    "command": "npx",')
        print('    "args": ["-y", "mcp-remote", "' + mcp_url + '", "--header",')
        print(f'              "Authorization: Bearer {settings.bearer_token}"]')
    else:
        print("  警告：当前为无认证模式，任何拿到地址的人都能读写你的工作区。")
        print("        仅建议在本机回环自测时使用。")
    print()
    print("  提示：隧道域名每次启动都会变，重启后需要在客户端更新一次地址。")
    print("        Ctrl+C 停止服务")
    print("=" * 68)
    print()


def run_cli(args: argparse.Namespace) -> int:
    settings = settings_from_args(args)

    def log(message: str) -> None:
        prefix = "" if message.startswith("[") else "[chatcode] "
        print(f"{prefix}{message}", flush=True)

    print("ChatGPT Code Bridge MCP — Web chat to local code")
    print(f"{APP_TITLE} v{__import__('chatcode').__version__}")
    print(f"工作区：{settings.workspace}")

    runtime = ServiceRuntime(settings, log=log)
    try:
        runtime.start()
        runtime.ready.wait(timeout=50)
        _print_ready(settings, settings.public_url or settings.discovered_public_url, log)
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        runtime.stop()
        print("已停止。")
    return 0
