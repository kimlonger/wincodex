"""全局配置与常量。"""

from __future__ import annotations

import argparse
import os
import secrets
import json
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

APP_NAME = "chatcode-mcp"
APP_TITLE = "ChatCodex"
VERSION = __version__

# MCP 端点。客户端连接的就是 https://<隧道域名>/mcp
MCP_ENDPOINT_PATH = "/mcp"

# 支持的 MCP 协议版本。客户端在 initialize 里声明版本，服务器回显它支持的那个。
# 用最通用的 2025-06-18 兜底，绝大多数云端客户端（ChatGPT / claude.ai / Grok）都认。
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")
DEFAULT_PROTOCOL_VERSION = "2025-06-18"

# 递归列举与搜索时跳过的目录。省上下文，也避免把仓库历史塞给模型。
EXCLUDED_DIR_NAMES = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "target",
        "dist",
        "build",
        ".idea",
        ".vscode",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".chatcode-tmp",
        ".gradle",
        ".mvn",
        "bin",
        "obj",
        ".next",
        ".nuxt",
        "out",
    }
)

# --- 输出限额：控制单次返回体积，直接决定体感速度 ---
MAX_TOOL_TEXT_CHARS = 60_000  # 工具返回给模型的文本上限
READ_MAX_LINES = 2_000        # read_file 单次最多行数
READ_MAX_BYTES = 400_000      # read_file 单次最多字节
SEARCH_MAX_MATCHES = 200      # search_text 最多匹配数
SEARCH_MAX_LINE_CHARS = 500   # 单行截断长度
EXEC_MAX_OUTPUT_BYTES = 32_000  # 命令输出上限
EXEC_DEFAULT_TIMEOUT = 60     # 命令默认超时（秒）
EXEC_MAX_TIMEOUT = 3600
GZIP_MIN_BYTES = 1_024        # 超过此体积才 gzip，小响应压缩反而慢

# --- 认证 ---
BEARER_TOKEN_BYTES = 32
OAUTH_TOKEN_TTL_SECONDS = 86_400  # 24 小时
OAUTH_CODE_TTL_SECONDS = 300
MAX_PENDING_CODES = 64
OAUTH_PASSWORD_BYTES = 12

# --- 危险命令：命中即拒绝，不给模型任何解释空间 ---
DESTRUCTIVE_COMMAND_PATTERNS = (
    r"\bformat\s+[a-z]:",
    r"\brm\s+-rf\s+/(\s|$)",
    r"\bdel\s+/[sfq]\b",
    r"\brmdir\s+/s\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bshutdown\b",
    r"\bdiag\s+",
    r":\(\)\s*\{",  # fork 炸弹
    r"\bgit\s+clean\s+-[^\s]*[fd]",
    r"\bgit\s+reset\s+--hard\b",
)

# 本地 HTTP 服务默认只绑回环地址，公网访问一律靠隧道。
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def default_workspace() -> Path:
    """未显式指定工作区时的兜底：当前目录。"""
    return Path.cwd().resolve()


def app_data_dir() -> Path:
    """存放自动下载的 cloudflared 等二进制。"""
    base = os.environ.get("CHATCODE_DATA_DIR")
    if base:
        return Path(base)
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / APP_NAME


@dataclass
class Settings:
    """运行时配置。"""

    workspace: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    # 认证模式：path（默认）/ oauth / bearer / none（仅回环自测）
    auth_mode: str = "path"
    bearer_token: str = field(default_factory=lambda: secrets.token_urlsafe(BEARER_TOKEN_BYTES))
    # path 模式的凭据：直接写进 URL 路径。客户端不用做任何授权动作，
    # 但不知道这串字符就访问不到，比裸奔强得多。
    path_token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    oauth_password: str = field(default_factory=lambda: secrets.token_urlsafe(OAUTH_PASSWORD_BYTES)[:16])
    oauth_token_ttl: int = OAUTH_TOKEN_TTL_SECONDS
    # 隧道后端：cloudflared / cpolar / frp / none
    tunnel: str = "cloudflared"
    # 让隧道进程走本地代理出站（如 Clash）。空字符串表示自动检测。
    proxy: str | None = None
    auto_proxy: bool = True
    # 固定公网地址（命名隧道或自建 frp 时使用），不为空则不再解析隧道输出
    public_url: str = ""
    # 自动发现的隧道地址与用户配置分开，避免重启时误判为固定地址。
    discovered_public_url: str = ""
    # 是否允许 exec_command
    allow_exec: bool = True
    read_only: bool = False
    allow_sensitive_files: bool = False
    projects: list[str] = field(default_factory=list)
    # 是否启用 gzip（大响应省带宽）
    gzip: bool = True
    # 隧道二进制路径（cloudflared / cpolar / frpc），为空则自动查找
    tunnel_binary: str = ""
    # cpolar 需要 authtoken 才能用固定域名；免费随机域名可留空
    cpolar_authtoken: str = ""
    # frp 服务端地址（自建时）
    frp_server_addr: str = ""
    frp_server_port: int = 7000
    frp_token: str = ""
    verbose: bool = False
    language_servers: dict[str, list[str]] = field(default_factory=dict)
    desktop_python: str = ""
    desktop_window: int = 0
    desktop_input: bool = False
    execution_mode: str = "host"
    sandbox_memory_mb: int = 1024
    storage_quota_mb: int = 2048

    def public_origin(self) -> str:
        """对外访问 origin，不带 /mcp。"""
        origin = self.public_url or self.discovered_public_url
        return origin.rstrip("/") if origin else f"http://{self.host}:{self.port}"

    def mcp_path(self) -> str:
        """实际生效的 MCP 端点路径。path 模式下令牌就在路径里。"""
        if self.auth_mode == "path":
            return f"{MCP_ENDPOINT_PATH}/{self.path_token}"
        return MCP_ENDPOINT_PATH

    def mcp_url(self) -> str:
        return self.public_origin() + self.mcp_path()


def parse_language_server(value: str):
    try:
        language, command = value.split("=", 1)
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,30}", language) is None:
            raise ValueError("语言 ID 无效")
        if command.lstrip().startswith("["):
            argv = json.loads(command)
        else:
            argv = shlex.split(command, posix=os.name != "nt")
            if os.name == "nt":
                argv = [part[1:-1] if len(part) > 1 and part[0] == part[-1] and part[0] in ('"', "'") else part for part in argv]
        if not isinstance(argv, list) or not 1 <= len(argv) <= 50 or not all(isinstance(part, str) and part and "\0" not in part for part in argv):
            raise ValueError("命令必须是非空 argv")
        return language, argv
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"语言服务格式为 LANG=COMMAND，例如 python=pylsp：{exc}") from exc


class _ArgumentParser(argparse.ArgumentParser):
    def parse_args(self, args=None, namespace=None):
        tokens = list(sys.argv[1:] if args is None else args)
        result = super().parse_args(tokens, namespace)
        provided = set()
        for token in tokens:
            option = token.split("=", 1)[0]
            action = self._option_string_actions.get(option)
            if action is None and token.startswith("-") and not token.startswith("--"):
                action = next((candidate for key, candidate in self._option_string_actions.items()
                               if len(key) == 2 and token.startswith(key) and len(token) > 2 and candidate.nargs != 0), None)
            if action is not None:
                provided.add(action.dest)
        result._provided_options = provided
        return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog=APP_NAME,
        description="把本地项目挂到 MCP 上，让网页版对话直接读写改造你的代码。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--workspace", "-w", default=None, help="要暴露的项目根目录，默认当前目录")
    parser.add_argument("--project", action="append", default=[], help="额外登记的项目目录，可重复；共享同一 MCP 入口")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"本地监听地址，默认 {DEFAULT_HOST}")
    parser.add_argument("--port", "-p", type=int, default=DEFAULT_PORT, help=f"本地端口，默认 {DEFAULT_PORT}")
    parser.add_argument(
        "--auth-mode",
        choices=("oauth", "path", "bearer", "none"),
        default="path",
        help=(
            "oauth: 网页版 ChatGPT 用（需输一次口令）；"
            "path: 无需授权，凭据藏在 URL 路径里（推荐自用）；"
            "bearer: 本地客户端用；none: 完全无认证，任何人拿到地址都能访问"
        ),
    )
    parser.add_argument(
        "--tunnel",
        choices=("cloudflared", "cpolar", "frp", "none"),
        default="cloudflared",
        help="内网穿透后端，默认 cloudflared",
    )
    parser.add_argument(
        "--proxy",
        default=None,
        help="隧道出站代理，如 http://127.0.0.1:7897。不指定时自动探测本机代理（可显著提速）",
    )
    parser.add_argument("--no-proxy", action="store_true", help="禁用自动探测代理，隧道直连")
    parser.add_argument("--public-url", default="", help="固定公网 origin；frp 用于配置域名，其他后端跳过隧道启动")
    parser.add_argument("--tunnel-binary", default="", help="隧道程序路径，不填则自动查找")
    parser.add_argument("--cpolar-authtoken", default="", help="cpolar 账号 authtoken")
    parser.add_argument("--frp-server-addr", default="", help="自建 frp 服务端地址")
    parser.add_argument("--frp-server-port", type=int, default=7000, help="自建 frp 服务端端口")
    parser.add_argument("--frp-token", default="", help="自建 frp 认证 token")
    parser.add_argument("--no-exec", action="store_true", help="禁用 exec_command 工具")
    parser.add_argument("--read-only", action="store_true", help="只允许只读工具，同时禁用命令和语言服务进程")
    parser.add_argument("--allow-sensitive-files", action="store_true", help="允许文件工具访问 .env/私钥等；不覆盖 .chatcodeignore")
    parser.add_argument("--no-gzip", action="store_true", help="关闭响应压缩")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出详细日志")
    parser.add_argument("--language-server", action="append", type=parse_language_server, default=[], help="可重复：LANG=COMMAND，例如 python=pylsp 或 typescript=typescript-language-server --stdio")
    parser.add_argument("--gui", action="store_true", help="启动图形界面")
    parser.add_argument("--desktop-python", default="", help="可选桌面后端的独立 Python 3.14 路径；默认不启用")
    parser.add_argument("--desktop-window", type=int, default=0, help="本次运行允许观察的窗口 HWND；只能本机配置")
    parser.add_argument("--desktop-input", action="store_true", help="本次运行允许向已绑定窗口输入；只读模式下无效")
    parser.add_argument("--execution-mode", choices=("host", "native"), default="host", help="native 使用 Windows AppContainer，无需额外沙箱软件；只在任务副本执行，失败拒绝回退")
    parser.add_argument("--sandbox-memory-mb", type=int, choices=range(128, 8193), metavar="128..8192", default=1024)
    parser.add_argument("--storage-quota-mb", type=int, choices=range(128, 65537), metavar="128..65536", default=2048, help="每项目副本容量预算，创建与启动前检查")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    workspace = Path(args.workspace).resolve() if args.workspace else default_workspace()
    if not workspace.is_dir():
        raise SystemExit(f"工作区不存在或不是目录：{workspace}")
    return Settings(
        workspace=workspace,
        host=args.host,
        port=args.port,
        auth_mode=args.auth_mode,
        tunnel=args.tunnel,
        proxy=args.proxy,
        auto_proxy=not args.no_proxy,
        public_url=args.public_url,
        allow_exec=not (args.no_exec or args.read_only),
        read_only=args.read_only,
        allow_sensitive_files=args.allow_sensitive_files,
        projects=list(args.project),
        gzip=not args.no_gzip,
        tunnel_binary=args.tunnel_binary,
        cpolar_authtoken=args.cpolar_authtoken,
        frp_server_addr=args.frp_server_addr,
        frp_server_port=args.frp_server_port,
        frp_token=args.frp_token,
        verbose=args.verbose,
        language_servers=dict(args.language_server),
        desktop_python=args.desktop_python,
        desktop_window=args.desktop_window,
        desktop_input=args.desktop_input and not args.read_only,
        execution_mode=args.execution_mode,
        sandbox_memory_mb=args.sandbox_memory_mb,
        storage_quota_mb=args.storage_quota_mb,
    )
