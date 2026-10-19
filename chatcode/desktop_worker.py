"""独立 Python 3.14 进程：复用 windows_mcp.uia，仅操作本机绑定的窗口。

JSON Lines 协议，不加载 MCP 上游的系统管理工具，不开启网络监听。
"""

import base64
from collections import deque
import contextlib
import hashlib
import io
import json
import os
import queue
import sys
import threading
import time


class Rejected(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class Native:
    def __init__(self):
        import ctypes as c
        from ctypes import wintypes as w
        self.c, self.w = c, w
        self.user = c.WinDLL("user32", use_last_error=True)
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("GetForegroundWindow", [], w.HWND), ("IsWindow", [w.HWND], w.BOOL),
            ("IsWindowVisible", [w.HWND], w.BOOL), ("IsIconic", [w.HWND], w.BOOL),
            ("GetWindowThreadProcessId", [w.HWND, c.POINTER(w.DWORD)], w.DWORD),
            ("GetWindowRect", [w.HWND, c.POINTER(w.RECT)], w.BOOL),
            ("GetWindowTextW", [w.HWND, w.LPWSTR, c.c_int], c.c_int),
            ("GetAncestor", [w.HWND, w.UINT], w.HWND), ("WindowFromPoint", [w.POINT], w.HWND),
            ("SetCursorPos", [c.c_int, c.c_int], w.BOOL), ("SetForegroundWindow", [w.HWND], w.BOOL),
            ("GetAsyncKeyState", [c.c_int], c.c_short),
        ):
            method = getattr(self.user, name)
            method.argtypes, method.restype = args, result
        self.kernel.CreateMutexW.argtypes = [c.c_void_p, w.BOOL, w.LPCWSTR]
        self.kernel.CreateMutexW.restype = w.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self.kernel.ReleaseMutex.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.mutex = self.kernel.CreateMutexW(None, False, "Local\\ChatCode.Desktop.v1")
        self.locked = False
        try:
            self.user.SetProcessDpiAwarenessContext(c.c_void_p(-4))
        except AttributeError:
            pass

    def identity(self, handle):
        import psutil
        if not self.user.IsWindow(handle) or not self.user.IsWindowVisible(handle):
            raise Rejected("WINDOW_GONE", "绑定窗口已关闭或不可见")
        pid = self.w.DWORD()
        self.user.GetWindowThreadProcessId(handle, self.c.byref(pid))
        return [pid.value, psutil.Process(pid.value).create_time()]

    def rect(self, handle):
        rect = self.w.RECT()
        if not self.user.GetWindowRect(handle, self.c.byref(rect)):
            raise Rejected("WINDOW_GONE", "无法读取窗口区域")
        value = [rect.left, rect.top, rect.right, rect.bottom]
        if rect.right <= rect.left or rect.bottom <= rect.top or self.user.IsIconic(handle):
            raise Rejected("WINDOW_MINIMIZED", "请先在本机恢复窗口")
        return value

    def title(self, handle):
        text = self.c.create_unicode_buffer(512)
        self.user.GetWindowTextW(handle, text, 512)
        return text.value

    def input_stamp(self):
        class LastInput(self.c.Structure):
            _fields_ = [("cbSize", self.w.UINT), ("dwTime", self.w.DWORD)]
        value = LastInput()
        value.cbSize = self.c.sizeof(value)
        if not self.user.GetLastInputInfo(self.c.byref(value)):
            raise Rejected("DESKTOP_UNAVAILABLE", "无法核对本机输入状态")
        return value.dwTime

    def check(self, handle, identity, *, foreground=True):
        if self.identity(handle) != identity:
            raise Rejected("WINDOW_CHANGED", "窗口身份已经变化，请在本机重新绑定")
        if foreground and self.user.GetForegroundWindow() != handle:
            raise Rejected("WINDOW_NOT_FOREGROUND", "绑定窗口不在前台；请在本机激活后重新观察")
        if self.user.GetAsyncKeyState(0x1B) & 0x8000:
            raise Rejected("USER_TAKEOVER", "本机 Escape 已按下，停止桌面操作")

    def acquire(self):
        if not self.locked:
            if self.kernel.WaitForSingleObject(self.mutex, 0) not in (0, 0x80):
                raise Rejected("DESKTOP_BUSY", "桌面正由另一个 ChatCode 服务使用")
            self.locked = True

    def release(self):
        if self.locked:
            self.kernel.ReleaseMutex(self.mutex)
            self.locked = False

    def windows(self):
        items = []
        callback_type = self.c.WINFUNCTYPE(self.w.BOOL, self.w.HWND, self.w.LPARAM)
        def visit(handle, _):
            if self.user.IsWindowVisible(handle) and len(items) < 200:
                title = self.title(handle)
                if title:
                    items.append({"handle": int(handle), "title": title, "foreground": self.user.GetForegroundWindow() == handle})
            return True
        callback = callback_type(visit)
        self.user.EnumWindows.argtypes = [callback_type, self.w.LPARAM]
        self.user.EnumWindows(callback, 0)
        return items

    def image(self, handle):
        # PrintWindow 按目标窗口 DPI 上下文绘制；否则旧式窗口在高 DPI 下会留下黑色空边。
        self.user.GetWindowDpiAwarenessContext.argtypes = [self.w.HWND]
        self.user.GetWindowDpiAwarenessContext.restype = self.c.c_void_p
        self.user.SetThreadDpiAwarenessContext.argtypes = [self.c.c_void_p]
        self.user.SetThreadDpiAwarenessContext.restype = self.c.c_void_p
        previous = self.user.SetThreadDpiAwarenessContext(self.user.GetWindowDpiAwarenessContext(handle))
        if not previous:
            raise Rejected("WINDOW_CAPTURE_FAILED", "无法切换窗口捕获 DPI 上下文")
        try:
            return self._image(handle)
        finally:
            self.user.SetThreadDpiAwarenessContext(previous)

    def _image(self, handle):
        from PIL import Image
        c, w = self.c, self.w
        rect = self.rect(handle)
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        if width * height > 20_000_000:
            raise Rejected("DESKTOP_IMAGE_TOO_LARGE", "窗口像素超过 2000 万")
        gdi = c.WinDLL("gdi32", use_last_error=True)
        self.user.GetWindowDC.argtypes, self.user.GetWindowDC.restype = [w.HWND], w.HDC
        self.user.ReleaseDC.argtypes = [w.HWND, w.HDC]
        self.user.PrintWindow.argtypes = [w.HWND, w.HDC, w.UINT]
        for name, args, result in (("CreateCompatibleDC", [w.HDC], w.HDC),
                ("CreateCompatibleBitmap", [w.HDC, c.c_int, c.c_int], w.HBITMAP),
                ("SelectObject", [w.HDC, w.HANDLE], w.HANDLE), ("DeleteObject", [w.HANDLE], w.BOOL),
                ("DeleteDC", [w.HDC], w.BOOL)):
            method = getattr(gdi, name)
            method.argtypes, method.restype = args, result
        dc = self.user.GetWindowDC(handle)
        target = gdi.CreateCompatibleDC(dc)
        bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
        previous = gdi.SelectObject(target, bitmap)
        try:
            if not self.user.PrintWindow(handle, target, 2):
                raise Rejected("WINDOW_CAPTURE_FAILED", "窗口不支持独立捕获；可关闭截图只读控件树，不回退到整屏捕获")
            class Header(c.Structure):
                _fields_ = [("size", w.DWORD), ("width", w.LONG), ("height", w.LONG), ("planes", w.WORD),
                    ("bits", w.WORD), ("compression", w.DWORD), ("image_size", w.DWORD),
                    ("x", w.LONG), ("y", w.LONG), ("used", w.DWORD), ("important", w.DWORD)]
            header = Header(c.sizeof(Header), width, -height, 1, 32)
            buffer = c.create_string_buffer(width * height * 4)
            gdi.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, c.c_void_p, c.c_void_p, w.UINT]
            gdi.SelectObject(target, previous)
            previous = None
            if gdi.GetDIBits(target, bitmap, 0, height, buffer, c.byref(header), 0) != height:
                raise Rejected("WINDOW_CAPTURE_FAILED", "窗口图像读取失败")
            return Image.frombytes("RGB", (width, height), buffer.raw, "raw", "BGRX")
        finally:
            if previous:
                gdi.SelectObject(target, previous)
            gdi.DeleteObject(bitmap)
            gdi.DeleteDC(target)
            self.user.ReleaseDC(handle, dc)

    def click(self, handle, x, y):
        root = self.user.GetAncestor(self.user.WindowFromPoint(self.w.POINT(x, y)), 2)
        if root != handle:
            raise Rejected("WINDOW_OCCLUDED", "目标控件被其他窗口遮挡，未点击")
        self.user.SetCursorPos(x, y)
        # 一次 SendInput 发送按下/抬起，避免超时中断留下按键保持状态。
        class Mouse(self.c.Structure):
            _fields_ = [("dx", self.w.LONG), ("dy", self.w.LONG), ("mouseData", self.w.DWORD),
                        ("flags", self.w.DWORD), ("time", self.w.DWORD), ("extra", self.c.c_size_t)]
        class Union(self.c.Union):
            _fields_ = [("mouse", Mouse)]
        class Input(self.c.Structure):
            _anonymous_ = ("data",)
            _fields_ = [("type", self.w.DWORD), ("data", Union)]
        events = (Input * 2)()
        events[0].mouse.flags, events[1].mouse.flags = 2, 4
        self.user.SendInput.argtypes = [self.w.UINT, self.c.POINTER(Input), self.c.c_int]
        if self.user.SendInput(2, events, self.c.sizeof(Input)) != 2:
            raise Rejected("DESKTOP_INPUT_FAILED", "系统拒绝鼠标输入；可能涉及不同权限级别")


class Worker:
    def __init__(self):
        from importlib.metadata import version
        if version("windows-mcp") != "0.8.8":
            raise Rejected("BACKEND_VERSION_MISMATCH", "需要固定版本 windows-mcp 0.8.8")
        import windows_mcp.uia as uia
        self.uia, self.native = uia, Native()
        self.handle, self.identity = 0, None
        self.nodes, self.state, self.stamp = {}, None, 0
        self.until = 0

    def expire(self):
        if self.native.locked and time.monotonic() >= self.until:
            self.native.release()
            self.state = None

    def bind(self, handle, activate=False):
        self.native.release()
        self.handle, self.identity = handle, self.native.identity(handle)
        self.nodes, self.state = {}, None
        if activate:
            self.native.user.SetForegroundWindow(handle)
        return {"handle": handle, "title": self.native.title(handle), "identity": self.identity}

    def tree(self, *, foreground=True):
        self.native.check(self.handle, self.identity, foreground=foreground)
        root = self.uia.ControlFromHandle(self.handle)
        pending, rows, nodes, visited = deque([(root, 0)]), [], {}, 0
        while pending and visited < 1000 and len(rows) < 200:
            node, depth = pending.popleft()
            visited += 1
            try:
                if node.ProcessId != self.identity[0]:
                    continue
                rect = node.BoundingRectangle
                bounds = [rect.left, rect.top, rect.right, rect.bottom]
                if node.IsPassword:
                    continue
                key = str(len(rows))
                row = {"element_id": key, "name": node.Name[:300], "role": node.ControlTypeName,
                       "enabled": node.IsEnabled, "bounds": bounds, "runtime_id": list(node.GetRuntimeId())}
                if row["role"] in ("EditControl", "DocumentControl"):
                    try:
                        row["value"] = node.GetValuePattern().Value[:2000]
                    except Exception:
                        pass
                rows.append(row)
                nodes[key] = node
                if len(json.dumps(rows, ensure_ascii=False).encode()) > 60_000:
                    rows.pop()
                    nodes.pop(key)
                    pending.append((node, depth))
                    break
                if depth < 20:
                    pending.extend((child, depth + 1) for child in node.GetChildren()[:200])
            except Exception:
                continue
        state = {"title": self.native.title(self.handle), "bounds": self.native.rect(self.handle), "elements": rows,
                 "foreground": self.native.user.GetForegroundWindow() == self.handle,
                 "truncated": bool(pending), "visited": visited}
        state["revision"] = hashlib.sha256(json.dumps(state, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.native.check(self.handle, self.identity, foreground=foreground)
        return state, nodes

    def observe(self, image=True):
        self.native.acquire()
        stamp = self.native.input_stamp()
        state, nodes = self.tree(foreground=False)
        blocks = []
        if image:
            frame = self.native.image(self.handle)
            frame.thumbnail((1440, 1000))
            stream = io.BytesIO()
            frame.save(stream, format="PNG")
            raw = stream.getvalue()
            if len(raw) > 2_000_000:
                raise Rejected("DESKTOP_IMAGE_TOO_LARGE", "窗口截图超过 2 MB，尝试关闭截图仅取控件树")
            blocks = [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(raw).decode()}]
            state["image_size"] = list(frame.size)
        self.native.check(self.handle, self.identity, foreground=False)
        if self.native.input_stamp() != stamp:
            raise Rejected("USER_TAKEOVER", "观察期间检测到本机输入，请停止交互后重新观察")
        self.nodes, self.state, self.stamp = nodes, state, self.native.input_stamp()
        return {"state": state, "content": blocks}

    def action(self, args):
        if self.state is None or self.state["revision"] != args["revision"]:
            raise Rejected("DESKTOP_STALE", "请先重新观察窗口")
        if self.native.input_stamp() != self.stamp:
            raise Rejected("USER_TAKEOVER", "观察后检测到本机输入，请重新观察；也可在本机暂停桌面功能")
        previous = self.state
        self.state = None
        current, nodes = self.tree()
        if current["revision"] != previous["revision"]:
            raise Rejected("DESKTOP_STALE", "窗口布局或内容已变化，未执行操作")
        element = args["element_id"]
        row = next((item for item in current["elements"] if item["element_id"] == element), None)
        if row is None or not row["enabled"]:
            raise Rejected("DESKTOP_ELEMENT_INVALID", "控件不存在或不可用")
        node = nodes[element]
        self.native.check(self.handle, self.identity)
        if args["action"] == "click":
            left, top, right, bottom = row["bounds"]
            win = current["bounds"]
            x, y = (left + right) // 2, (top + bottom) // 2
            if not (win[0] <= x < win[2] and win[1] <= y < win[3]):
                raise Rejected("DESKTOP_ELEMENT_INVALID", "控件不在已绑定窗口区域")
            self.native.click(self.handle, x, y)
        elif args["action"] == "set_text":
            if row["role"] != "EditControl":
                raise Rejected("DESKTOP_ELEMENT_UNSUPPORTED", "首版只允许向普通编辑框设置文本")
            try:
                pattern = node.GetValuePattern()
                if pattern.IsReadOnly:
                    raise Rejected("DESKTOP_ELEMENT_UNSUPPORTED", "编辑框为只读")
                pattern.SetValue(args["text"])
            except Rejected:
                raise
            except Exception:
                raise Rejected("DESKTOP_ELEMENT_UNSUPPORTED", "编辑框不支持 UI Automation ValuePattern") from None
        else:
            raise Rejected("INVALID_ARGUMENT", "不支持此桌面动作")
        time.sleep(0.1)
        self.native.check(self.handle, self.identity)
        return {"performed": True, "verification_required": True, "notice": "请重新 desktop_observe 核对实际结果；不自动推断业务成功"}

    def request(self, method, args):
        if method == "windows":
            return {"windows": self.native.windows()}
        if method == "bind":
            return self.bind(args["handle"], args.get("activate", False))
        if method == "observe":
            result = self.observe(args.get("image", True))
            self.until = time.monotonic() + 30
            return result
        if method == "action":
            self.expire()
            result = self.action(args)
            self.until = time.monotonic() + 30
            return result
        if method == "release":
            self.native.release()
            self.state = None
            return {"released": True}
        raise Rejected("INVALID_METHOD", "未知桌面方法")


def main():
    protocol = sys.stdout
    sys.stdout = sys.stderr  # 第三方库日志不得污染协议。
    try:
        worker = Worker()
        pending = queue.Queue(maxsize=4)
        def read():
            while True:
                line = sys.stdin.buffer.readline(20_001)
                if not line or len(line) > 20_000:
                    break
                pending.put(line)
            pending.put(None)
        threading.Thread(target=read, daemon=True).start()
        while True:
            try:
                line = pending.get(timeout=0.5)
            except queue.Empty:
                worker.expire()
                continue
            if line is None:
                break
            request = {}
            try:
                request = json.loads(line)
                result = worker.request(request["method"], request.get("args", {}))
                response = {"id": request["id"], "result": result}
            except Exception as exc:
                if request.get("method") in ("observe", "action"):
                    worker.native.release()
                    worker.state = None
                response = {"id": request.get("id"), "error": {"code": getattr(exc, "code", "DESKTOP_BACKEND_ERROR"), "message": str(exc)[:1000]}}
            protocol.write(json.dumps(response, ensure_ascii=True) + "\n")
            protocol.flush()
    finally:
        if "worker" in locals():
            worker.native.release()


if __name__ == "__main__":
    main()
