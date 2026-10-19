"""仅供显式桌面集成验证的自有 Win32 窗口，不触及其他应用。"""

import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import sys
import time


def main():
    user, kernel = c.WinDLL("user32"), c.WinDLL("kernel32")
    callback_type = c.WINFUNCTYPE(c.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    user.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user.DefWindowProcW.restype = c.c_ssize_t
    user.SetWindowTextW.argtypes = [w.HWND, w.LPCWSTR]
    count, label = 0, None
    def message(handle, msg, wp, lp):
        nonlocal count
        if msg == 0x8001:
            activate(handle)
            return int(user.GetForegroundWindow() == handle)
        if msg == 0x111 and wp & 0xffff == 101:
            count += 1
            user.SetWindowTextW(label, "Clicked " + str(count))
            return 0
        if msg == 2:
            user.PostQuitMessage(0)
            return 0
        return user.DefWindowProcW(handle, msg, wp, lp)
    callback = callback_type(message)
    class WindowClass(c.Structure):
        _fields_ = [("style", w.UINT), ("proc", callback_type), ("clsExtra", c.c_int), ("wndExtra", c.c_int),
                    ("instance", w.HINSTANCE), ("icon", w.HICON), ("cursor", w.HANDLE), ("background", w.HBRUSH),
                    ("menu", w.LPCWSTR), ("name", w.LPCWSTR)]
    kernel.GetModuleHandleW.restype = w.HMODULE
    instance = kernel.GetModuleHandleW(None)
    name = "ChatCodeDesktopVerification"
    klass = WindowClass(0, callback, 0, 0, instance, None, None, c.c_void_p(6), None, name)
    user.RegisterClassW(c.byref(klass))
    user.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
        c.c_int, c.c_int, c.c_int, c.c_int, w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p]
    user.CreateWindowExW.restype = w.HWND
    title = sys.argv[2] if len(sys.argv) > 2 else "ChatCode Desktop Self-Test"
    handle = user.CreateWindowExW(8, name, title, 0x10cf0000,
                                 120, 120, 600, 330, None, None, instance, None)
    user.ChangeWindowMessageFilterEx.argtypes = [w.HWND, w.UINT, w.DWORD, c.c_void_p]
    user.ChangeWindowMessageFilterEx(handle, 0x8001, 1, None)
    edit = user.CreateWindowExW(0x200, "EDIT", "initial", 0x50010080, 25, 35, 480, 40, handle, c.c_void_p(102), instance, None)
    user.CreateWindowExW(0, "BUTTON", "Test action", 0x50010000, 25, 105, 160, 42, handle, c.c_void_p(101), instance, None)
    label = user.CreateWindowExW(0, "STATIC", "Clicked 0", 0x50000000, 25, 180, 400, 32, handle, None, instance, None)
    user.ShowWindow(handle, 5)
    user.SetForegroundWindow.argtypes = [w.HWND]
    user.BringWindowToTop.argtypes = [w.HWND]
    user.SetActiveWindow.argtypes = [w.HWND]
    user.SetFocus.argtypes = [w.HWND]
    user.GetForegroundWindow.restype = w.HWND
    user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    user.AttachThreadInput.argtypes = [w.DWORD, w.DWORD, w.BOOL]
    def activate(target):
        current = kernel.GetCurrentThreadId()
        foreground = user.GetWindowThreadProcessId(user.GetForegroundWindow(), None)
        attached = foreground != current and user.AttachThreadInput(current, foreground, True)
        try:
            user.ShowWindow(target, 9)
            for _ in range(5):
                user.BringWindowToTop(target)
                user.SetForegroundWindow(target)
                user.SetActiveWindow(target)
                user.SetFocus(target)
                if user.GetForegroundWindow() == target:
                    break
                time.sleep(0.05)
        finally:
            if attached:
                user.AttachThreadInput(current, foreground, False)
    activate(handle)
    Path(sys.argv[1]).write_text(json.dumps({"handle": int(handle), "edit": int(edit)}))
    message_value = w.MSG()
    while user.GetMessageW(c.byref(message_value), None, 0, 0) > 0:
        user.TranslateMessage(c.byref(message_value))
        user.DispatchMessageW(c.byref(message_value))


if __name__ == "__main__":
    main()
