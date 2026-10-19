"""内网穿透封装：把本地 MCP 服务变成公网 HTTPS 地址。

为什么隧道能提速（这是本模块的关键设计）：
Cloudflare Quick Tunnel 的数据链路是两段——
  段1：ChatGPT 云端 → Cloudflare 边缘（都在海外，很快）
  段2：你的电脑 → Cloudflare 边缘（跨境，这段才是瓶颈）
cloudflared 支持标准环境变量 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 出站
（见 cloudflared PR #1514 的 proxyAwareDialer）。所以只要把本地代理地址喂给它，
段2 就走你的优质线路，速度和稳定性都能明显改善。这就是 --proxy 的作用。

后端可切换：cloudflared（默认，海外入口，对云端模型最友好）/ cpolar（国内入口）
/ frp（自建服务器，最可控）/ none（已有公网地址时）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Settings, app_data_dir
from .proxy import COMMON_PROXY_PORTS, detect_proxy, display_proxy, port_is_open

LogFn = Callable[[str], None]

# 各后端的公网地址特征
CLOUDFLARE_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
CPOLAR_URL_RE = re.compile(r"https?://[a-z0-9-]+\.(?:cpolar\.cn|cpolar\.top|cpolar\.io|cpolar\.pro)")
FRP_URL_RE = re.compile(r"https?://[a-z0-9.-]+:\d+")

CLOUDFLARE_DOWNLOAD_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
)
CLOUDFLARE_DOWNLOAD_URL_POSIX = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
)


def _default_log(message: str) -> None:
    sys.stderr.write(f"[tunnel] {message}\n")
    sys.stderr.flush()


def detect_local_proxy() -> str | None:
    """兼容旧接口；优先配置，其次才扫描常用端口。"""
    return detect_proxy().proxy


def find_binary(name: str, explicit: str = "") -> str | None:
    """按 显式路径 → PATH → 项目 bin/ → 数据目录 的顺序找可执行文件。"""
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return str(candidate)
    found = shutil.which(name)
    if found:
        return found
    # PyInstaller onefile：资源被解压到 sys._MEIPASS
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for suffix in (".exe", ""):
            candidate = Path(meipass) / "bin" / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate)
    here = Path(__file__).resolve().parent.parent / "bin"
    for suffix in (".exe", ""):
        candidate = here / f"{name}{suffix}"
        if candidate.is_file():
            return str(candidate)
    cached = app_data_dir() / "bin" / (f"{name}.exe" if os.name == "nt" else name)
    if cached.is_file():
        return str(cached)
    return None


def download_cloudflared(log: LogFn = _default_log) -> str | None:
    """下载 cloudflared 到数据目录。首次运行时用，之后直接用缓存。"""
    target_dir = app_data_dir() / "bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    is_windows = os.name == "nt"
    target = target_dir / ("cloudflared.exe" if is_windows else "cloudflared")
    url = CLOUDFLARE_DOWNLOAD_URL if is_windows else CLOUDFLARE_DOWNLOAD_URL_POSIX
    log(f"未找到 cloudflared，正在下载（约 40MB，慢的话可以挂代理重试）…")
    try:
        urllib.request.urlretrieve(url, str(target))
    except Exception as exc:
        log(f"下载失败：{exc}")
        log("可手动下载后放到项目 bin/ 目录，或用 --tunnel-binary 指定路径")
        return None
    if not is_windows:
        target.chmod(0o755)
    log(f"已下载到 {target}")
    return str(target)


@dataclass
class Tunnel:
    """一个正在运行的隧道进程。"""

    backend: str
    public_url: str = ""
    process: subprocess.Popen | None = None
    _reader: threading.Thread | None = field(default=None, repr=False)
    _log: LogFn = field(default=_default_log, repr=False)
    _url_event: threading.Event = field(default_factory=threading.Event, repr=False)

    def wait_for_url(self, timeout: float = 40.0) -> str:
        """阻塞等待公网地址出现。"""
        self._url_event.wait(timeout)
        return self.public_url

    def stop(self) -> None:
        if self.process is None:
            return
        proc = self.process
        self.process = None
        try:
            if os.name == "nt":
                # 必须杀整棵树，否则 cloudflared 的子进程会留着
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=8,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception as exc:
            self._log(f"停止隧道时出错：{exc}")

    def _pump(self, patterns: tuple[re.Pattern, ...], stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip()
                if not line:
                    continue
                if self._log and _is_useful(line):
                    self._log(_shorten(line))
                if not self.public_url:
                    for pattern in patterns:
                        match = pattern.search(line)
                        if match:
                            self.public_url = match.group(0)
                            self._url_event.set()
                            break
        except Exception:
            return

    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None


def _is_useful(line: str) -> bool:
    noise = ("Thank you for trying", "ICMP proxy", "Cannot determine default", "metrics server", "Tunnel connection curve")
    return not any(n in line for n in noise)


def _shorten(line: str) -> str:
    match = re.match(r"^\S+Z\s+(INF|WRN|ERR)\s+(.*)$", line)
    text = match.group(2) if match else line
    return text[:160]


def _spawn(cmd: list[str], env: dict[str, str] | None, log: LogFn) -> subprocess.Popen:
    kwargs: dict = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "env": env,
    }
    if os.name == "nt":
        # 不弹黑框
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log("执行：" + " ".join(cmd))
    return subprocess.Popen(cmd, **kwargs)


def _proxy_env(proxy: str | None) -> dict[str, str] | None:
    """把代理塞进子进程环境。cloudflared 认这些标准变量。"""
    if not proxy:
        return None
    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env[key] = proxy
    return env


def start_tunnel(settings: Settings, log: LogFn = _default_log) -> Tunnel | None:
    """按配置启动隧道。返回 Tunnel（public_url 可能在后台线程里稍后才填上）。"""
    backend = settings.tunnel

    if backend == "none":
        log("未启用隧道（--tunnel none），请自行保证公网可达")
        tunnel = Tunnel(backend="none", public_url=settings.public_url, _log=log)
        tunnel._url_event.set()
        return tunnel

    if settings.public_url and backend != "frp":
        log(f"已指定公网地址 {settings.public_url}，跳过隧道启动")
        tunnel = Tunnel(backend=backend, public_url=settings.public_url, _log=log)
        tunnel._url_event.set()
        return tunnel

    # 决定代理：显式指定 > 自动探测（除非 --no-proxy）
    proxy = settings.proxy
    if proxy is None and settings.auto_proxy:
        detection = detect_proxy()
        proxy = detection.proxy
        if proxy:
            log(f"检测到代理 {display_proxy(proxy)}（来源：{detection.source}）；将代理设置传给隧道进程")
        else:
            log(detection.message)
    env = _proxy_env(proxy)
    if proxy is None and not settings.auto_proxy:
        env = os.environ.copy()
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            env.pop(key, None)

    if backend == "cloudflared":
        return _start_cloudflared(settings, env, log, proxy)
    if backend == "cpolar":
        return _start_cpolar(settings, env, log)
    if backend == "frp":
        return _start_frp(settings, log)
    log(f"未知隧道后端：{backend}")
    return None


def _start_cloudflared(settings: Settings, env: dict[str, str] | None, log: LogFn, proxy: str | None) -> Tunnel | None:
    binary = find_binary("cloudflared", settings.tunnel_binary)
    if binary is None:
        binary = download_cloudflared(log)
    if binary is None:
        return None

    target = f"http://127.0.0.1:{settings.port}"
    cmd = [binary, "tunnel", "--url", target, "--no-autoupdate"]
    try:
        proc = _spawn(cmd, env, log)
    except Exception as exc:
        log(f"启动 cloudflared 失败：{exc}")
        return None

    tunnel = Tunnel(backend="cloudflared", process=proc, _log=log)
    reader = threading.Thread(
        target=tunnel._pump,
        args=((CLOUDFLARE_URL_RE,), proc.stdout),
        daemon=True,
        name="cloudflared-output",
    )
    tunnel._reader = reader
    reader.start()
    return tunnel


def _start_cpolar(settings: Settings, env: dict[str, str] | None, log: LogFn) -> Tunnel | None:
    binary = find_binary("cpolar", settings.tunnel_binary)
    if binary is None:
        log("未找到 cpolar，请先安装或用 --tunnel-binary 指定")
        return None
    cmd = [binary, "http", str(settings.port)]
    if settings.cpolar_authtoken:
        cmd.extend(["-authtoken", settings.cpolar_authtoken])
    try:
        proc = _spawn(cmd, env, log)
    except Exception as exc:
        log(f"启动 cpolar 失败：{exc}")
        return None
    tunnel = Tunnel(backend="cpolar", process=proc, _log=log)
    reader = threading.Thread(
        target=tunnel._pump,
        args=((CPOLAR_URL_RE,), proc.stdout),
        daemon=True,
        name="cpolar-output",
    )
    tunnel._reader = reader
    reader.start()
    return tunnel


def _start_frp(settings: Settings, log: LogFn) -> Tunnel | None:
    binary = find_binary("frpc", settings.tunnel_binary)
    if binary is None:
        log("未找到 frpc，请先安装或用 --tunnel-binary 指定")
        return None
    if not settings.frp_server_addr:
        log("使用 frp 需要提供 --frp-server-addr（服务端地址）")
        return None

    config = [
        f"serverAddr = \"{settings.frp_server_addr}\"",
        f"serverPort = {settings.frp_server_port}",
    ]
    if settings.frp_token:
        config.append(f"auth.token = \"{settings.frp_token}\"")
    config.append("")
    config.append("[[proxies]]")
    config.append(f"name = \"chatcode-{settings.port}\"")
    config.append("type = \"http\"")
    config.append("localIP = \"127.0.0.1\"")
    config.append(f"localPort = {settings.port}")
    if settings.public_url:
        host = settings.public_url.split("//")[-1].rstrip("/")
        config.append(f"customDomains = [\"{host}\"]")

    config_path = app_data_dir() / "frpc.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(config) + "\n", encoding="utf-8")
    log(f"已生成 frpc 配置：{config_path}")

    try:
        proc = _spawn([binary, "-c", str(config_path)], None, log)
    except Exception as exc:
        log(f"启动 frpc 失败：{exc}")
        return None
    tunnel = Tunnel(backend="frp", process=proc, public_url=settings.public_url, _log=log)
    if settings.public_url:
        tunnel._url_event.set()
    reader = threading.Thread(
        target=tunnel._pump,
        args=((FRP_URL_RE,), proc.stdout),
        daemon=True,
        name="frpc-output",
    )
    tunnel._reader = reader
    reader.start()
    return tunnel
