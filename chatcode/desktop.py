"""连接级可选桌面桥；本机绑定、作用域租约、单次观察票据及动作去重。"""

import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import time

from .config import app_data_dir
from .files import atomic_write_bytes
from .processes import _WindowsJob
from .tool_types import ToolError


class Backend:
    def __init__(self, python):
        source = (Path(sys._MEIPASS) / "desktop_backend" / "desktop_worker.py" if getattr(sys, "frozen", False)
                  else Path(__file__).with_name("desktop_worker.py"))
        env = os.environ.copy()
        env.update(ANONYMIZED_TELEMETRY="false", DO_NOT_TRACK="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
        self.process = subprocess.Popen([str(python), "-I", "-u", str(source)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=str(app_data_dir()),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            self.job = _WindowsJob(self.process) if os.name == "nt" else None
        except OSError:
            self.process.kill()
            self.process.wait(3)
            raise
        self.responses, self.next_id, self.error_tail = queue.Queue(maxsize=4), 0, bytearray()
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._stderr, daemon=True).start()

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(6_000_001)
                if not line or len(line) > 6_000_000:
                    break
                self.responses.put_nowait(json.loads(line))
        except (ValueError, queue.Full, OSError):
            pass
        try:
            self.responses.put_nowait(None)
        except queue.Full:
            pass

    def _stderr(self):
        try:
            while data := self.process.stderr.read(4096):
                self.error_tail.extend(data)
                del self.error_tail[:-8192]
        except (OSError, ValueError):
            pass

    def request(self, method, args, timeout=15):
        self.next_id += 1
        request = {"id": self.next_id, "method": method, "args": args}
        try:
            self.process.stdin.write((json.dumps(request, ensure_ascii=True) + "\n").encode())
            self.process.stdin.flush()
            result = self.responses.get(timeout=timeout)
        except (OSError, ValueError, queue.Empty):
            self.close()
            raise ToolError("DESKTOP_TIMEOUT", "桌面后端无响应，已停止；操作结果可能不明，不会自动重试") from None
        if not isinstance(result, dict) or result.get("id") != self.next_id:
            self.close()
            tail = self.error_tail.decode("utf-8", "replace")[-500:]
            raise ToolError("DESKTOP_BACKEND_CLOSED", "桌面后端退出或协议异常，请检查独立运行环境后在本机重新启用" + (f"；后端信息：{tail}" if tail else ""))
        if "error" in result:
            raise ToolError(result["error"].get("code", "DESKTOP_BACKEND_ERROR"), result["error"].get("message", "桌面后端错误"))
        if not isinstance(result.get("result"), dict):
            raise ToolError("DESKTOP_PROTOCOL_ERROR", "桌面结果格式无效")
        return result["result"]

    def close(self):
        if self.process.poll() is None:
            if self.job is not None:
                self.job.close()
                self.job = None
            else:
                self.process.kill()
        if self.job is not None:
            self.job.close()
            self.job = None
        try:
            self.process.wait(3)
        except subprocess.TimeoutExpired:
            self.process.kill()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass


class DesktopBridge:
    def __init__(self, settings, factory=Backend):
        self.settings, self.factory = settings, factory
        self.lock = threading.Lock()
        self.backend, self.bound, self.owner, self.ticket = None, None, None, None
        self.python, self.handle = settings.desktop_python, settings.desktop_window
        self.project = str(settings.workspace)
        self.input = settings.desktop_input and not settings.read_only
        self.paused, self.closed, self.expires = not bool(self.python and self.handle), False, 0
        self.run_id = secrets.token_hex(16)

    def status(self):
        return {"configured": bool(self.python), "paused": self.paused, "window": self.bound,
                "input_enabled": self.input and not self.settings.read_only and not self.paused and not self.closed, "authorized_project": self.project,
                "lease_active": self.owner is not None and time.monotonic() < self.expires,
                "os_sandbox": False, "backend": "windows-mcp.uia", "backend_version": "0.8.8"}

    def _ensure(self):
        if self.closed:
            raise ToolError("SERVICE_STOPPING", "服务正在停止")
        if not self.python:
            raise ToolError("DESKTOP_DISABLED", "尚未在本机配置桌面运行环境")
        if self.backend is None:
            try:
                app_data_dir().mkdir(parents=True, exist_ok=True)
                self.backend = self.factory(self.python)
                if self.handle:
                    self.bound = self.backend.request("bind", {"handle": self.handle})
            except OSError as exc:
                self.pause()
                raise ToolError("DESKTOP_START_FAILED", f"无法启动独立桌面后端：{exc}") from None
            except Exception:
                self.pause()
                raise
        return self.backend

    def local_windows(self, python=None):
        with self.lock:
            if python and python != self.python:
                self.pause()
                self.python = python
            if self.backend is None:
                self.handle, self.bound, self.paused = 0, None, True
            return self._ensure().request("windows", {})["windows"]

    def bind_local(self, python, handle, project, allow_input=False, activate=True):
        with self.lock:
            self.pause()
            self.python, self.handle, self.project = python, handle, str(project)
            self.input = allow_input and not self.settings.read_only
            try:
                backend = self._ensure()
                self.bound = backend.request("bind", {"handle": handle, "activate": activate})
                self.paused = False
                return self.status()
            except Exception:
                self.pause()
                raise

    def pause(self):
        # 不等待会话锁；本机停止入口可中断正在执行或挂起的后端。
        self.paused, self.owner, self.ticket = True, None, None
        backend, self.backend = self.backend, None
        if backend is not None:
            backend.close()

    def _scope(self, scope):
        if self.paused or self.closed:
            raise ToolError("DESKTOP_PAUSED", "桌面功能未启用或已在本机暂停")
        if scope[0] != self.project:
            raise ToolError("DESKTOP_PROJECT_DENIED", "该窗口绑定另一个项目，需要在本机重新绑定")
        if self.owner is not None and time.monotonic() >= self.expires:
            self._ensure().request("release", {})
            self.owner, self.ticket = None, None
        if self.owner is not None and self.owner != scope:
            raise ToolError("DESKTOP_IN_USE", "桌面窗口正由同项目的另一个任务使用；先释放或等待租约到期")

    def observe(self, scope, image=True):
        if not self.lock.acquire(blocking=False):
            raise ToolError("DESKTOP_BUSY", "桌面请求正在执行")
        try:
            self._scope(scope)
            result = self._ensure().request("observe", {"image": image})
            if self.paused:
                raise ToolError("DESKTOP_PAUSED", "观察期间已在本机暂停")
            blocks = result.get("content", [])
            if not isinstance(blocks, list) or len(blocks) > 1:
                raise ToolError("DESKTOP_PROTOCOL_ERROR", "单次窗口观察最多返回一张图片")
            if not isinstance(result.get("state"), dict) or len(json.dumps(result["state"]).encode()) > 100_000:
                raise ToolError("DESKTOP_PROTOCOL_ERROR", "窗口控件树格式无效或超过 100 KB")
            for block in blocks:
                if block.get("type") != "image" or block.get("mimeType") != "image/png":
                    raise ToolError("DESKTOP_PROTOCOL_ERROR", "不支持此桌面内容格式")
                try:
                    raw = base64.b64decode(block["data"], validate=True)
                    if len(raw) > 2_000_000 or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ValueError()
                except (ValueError, KeyError):
                    raise ToolError("DESKTOP_PROTOCOL_ERROR", "窗口截图无效或超出预算") from None
            self.owner, self.expires = scope, time.monotonic() + 30
            self.ticket = secrets.token_urlsafe(24)
            return {"snapshot_token": self.ticket, "valid_for_seconds": 30, "observation": result["state"],
                    "input_enabled": self.input, "os_sandbox": False}, blocks
        except Exception as exc:
            self.ticket = None
            if getattr(exc, "code", "") in ("DESKTOP_TIMEOUT", "DESKTOP_BACKEND_CLOSED"):
                self.pause()
            raise
        finally:
            self.lock.release()

    def action(self, scope, args):
        if not self.lock.acquire(blocking=False):
            raise ToolError("DESKTOP_BUSY", "桌面请求正在执行")
        try:
            self._scope(scope)
            if not self.input or self.settings.read_only:
                raise ToolError("DESKTOP_INPUT_DISABLED", "本机未授权桌面输入")
            key = hashlib.sha256(json.dumps([scope, args["operation_id"]]).encode()).hexdigest()
            directory = app_data_dir() / "desktop_operations"
            directory.mkdir(parents=True, exist_ok=True)
            record_path = directory / (key + ".json")
            fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
            if record_path.exists():
                try:
                    if record_path.is_symlink() or record_path.stat().st_size > 16_000:
                        raise ValueError()
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    if not isinstance(record, dict):
                        raise ValueError()
                except (OSError, ValueError):
                    raise ToolError("OPERATION_UNAVAILABLE", "桌面动作去重记录损坏，未重复执行，请在本机核对") from None
                if record.get("fingerprint") != fingerprint:
                    raise ToolError("OPERATION_ID_REUSED", "桌面操作 ID 已被另一请求使用")
                if record.get("run_id") != self.run_id or record.get("state") != "finished":
                    raise ToolError("OPERATION_UNAVAILABLE", "旧桌面动作已预约或结果不明，请核对窗口，不会自动重做")
                return {**record["result"], "replayed": True}
            if self.ticket is None or not secrets.compare_digest(self.ticket, args["snapshot_token"]):
                raise ToolError("DESKTOP_STALE", "观察票据已过期或已使用，请先重新观察")
            if sum(1 for _ in directory.glob("*.json")) >= 10_000:
                raise ToolError("DESKTOP_OPERATION_LIMIT", "桌面动作去重记录已达 10000 条，需要在本机整理历史")
            self.ticket = None
            record = {"fingerprint": fingerprint, "run_id": self.run_id, "state": "reserved"}
            atomic_write_bytes(record_path, json.dumps(record).encode(), mode=0o600)
            result = self._ensure().request("action", {key: value for key, value in args.items() if key not in ("snapshot_token", "operation_id")})
            result["operation_id"] = args["operation_id"]
            record.update(state="finished", result=result)
            atomic_write_bytes(record_path, json.dumps(record).encode(), mode=0o600)
            self.expires = time.monotonic() + 30
            return result
        except ToolError as exc:
            if exc.code in ("DESKTOP_TIMEOUT", "DESKTOP_BACKEND_CLOSED"):
                self.pause()
            raise
        finally:
            self.lock.release()

    def release(self, scope):
        if not self.lock.acquire(blocking=False):
            raise ToolError("DESKTOP_BUSY", "桌面请求正在执行；本机暂停入口可立即停止")
        try:
            self._scope(scope)
            if self.backend is not None:
                self.backend.request("release", {})
            self.owner, self.ticket = None, None
            return {"released": True}
        finally:
            self.lock.release()

    def close(self):
        self.closed = True
        self.pause()
