"""CLI / GUI 共用的服务生命周期，负责隧道监控、重试与停止。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .config import Settings
from .mcp_http import create_server
from .tunnel import start_tunnel


class ServiceRuntime:
    def __init__(self, settings: Settings, *, log: Callable[[str], None] = print,
                 on_ready: Callable[[str], None] | None = None, retry_delay: float = 2.0,
                 tunnel_timeout: float = 45.0) -> None:
        self.settings = settings
        self.server = None
        self.tunnel = None
        self.ready = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._http_thread = None
        self._monitor_thread = None
        self._log_callback = log
        self._on_ready = on_ready
        self.retry_delay = retry_delay
        self.tunnel_timeout = tunnel_timeout

    def _log(self, message: str) -> None:
        if not self._stop.is_set():
            try:
                self._log_callback(message)
            except Exception:
                # 输出窗口关闭或日志回调失败，不能阻断停止/恢复逻辑。
                pass

    def start(self) -> None:
        with self._lock:
            if self.server is not None:
                raise RuntimeError("服务已经运行")
            self._stop.clear()
            self.ready.clear()
            self.settings.discovered_public_url = ""
            self.server = create_server(self.settings)
            self._http_thread = threading.Thread(target=self.server.serve_forever,
                                                 kwargs={"poll_interval": 0.1}, daemon=True)
            self._http_thread.start()
            self._log(f"本地服务已启动：http://{self.settings.host}:{self.settings.port}")
            for item in self.server.recovery_report:
                self._log(f"编辑恢复记录 {item['change_id']}：{item['state']}")
            self._monitor_thread = threading.Thread(target=self._monitor, daemon=True, name="chatcode-tunnel-monitor")
            self._monitor_thread.start()

    def _notify(self, url: str) -> None:
        if self._stop.is_set():
            return
        self.settings.discovered_public_url = url
        self.ready.set()
        if self._on_ready is not None:
            try:
                self._on_ready(url)
            except Exception as exc:
                # GUI 已关闭时，排队中的通知不能影响进程清理。
                self._log(f"状态通知未完成：{exc}")

    def _monitor(self) -> None:
        while not self._stop.is_set():
            tunnel = None
            try:
                tunnel = start_tunnel(self.settings, log=self._log)
                with self._lock:
                    if self._stop.is_set():
                        if tunnel is not None:
                            tunnel.stop()
                        return
                    self.tunnel = tunnel
                if tunnel is None:
                    self._notify("")
                else:
                    deadline = time.monotonic() + self.tunnel_timeout
                    while not tunnel.public_url and time.monotonic() < deadline:
                        if self._stop.wait(0.05):
                            return
                        if tunnel.process is None or not tunnel.alive():
                            break
                    if tunnel.public_url:
                        self._notify(tunnel.public_url)
                        self._log(f"MCP 接入地址：{self.settings.mcp_url()}")
                        if tunnel.process is None:
                            return
                        while tunnel.alive():
                            if self._stop.wait(0.2):
                                return
                    else:
                        self._notify("")
                        if tunnel.process is None:
                            return
                        self._log("隧道尚未就绪，将重新尝试连接")
                    tunnel.stop()
                self._notify("")
                self._log("隧道已退出或启动失败，稍后重试；地址变化后需要更新客户端")
            except Exception as exc:
                if tunnel is not None:
                    tunnel.stop()
                self._log(f"隧道启动失败：{exc}")
                self._notify("")
            if self._stop.wait(self.retry_delay):
                return

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            tunnel, self.tunnel = self.tunnel, None
            server, self.server = self.server, None
        try:
            if tunnel is not None:
                tunnel.stop()
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
                for error in server.projects.shutdown_errors:
                    self._log(error)
        for thread in (self._http_thread, self._monitor_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2)
        self.settings.discovered_public_url = ""

    def switch_workspace(self, path, *, settings=None):
        with self._lock:
            if self.server is None or self._stop.is_set():
                raise RuntimeError("服务未运行")
            project = self.server.switch_workspace(path, settings=settings)
        self._log(f"当前项目：{project.workspace.root}；连接地址保持不变")
        return project
