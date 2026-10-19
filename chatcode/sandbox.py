"""Windows AppContainer：允许公网出站，挂起创建并加入有界 Job，拒绝权限回退。"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import uuid

from .files import FileError
from .maintenance import check_quota, tree_usage

NETWORK_CAPABILITIES = ("internetClient",)


def network_policy():
    return {"network": "internet", "network_capabilities": list(NETWORK_CAPABILITIES),
            "loopback": "windows_appcontainer_policy"}


def runtime_dir():
    base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
    return base / "bin" / "python-sandbox"


def _api():
    if os.name != "nt":
        raise FileError("SANDBOX_UNAVAILABLE", "原生沙箱需要 Windows 10/11；不会退回宿主权限")
    import ctypes as c
    from ctypes import wintypes as w
    k, u, a = c.WinDLL("kernel32", use_last_error=True), c.WinDLL("userenv", use_last_error=True), c.WinDLL("advapi32", use_last_error=True)
    class SA(c.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]
    class SI(c.Structure):
        _fields_ = [("cb", w.DWORD), ("reserved", w.LPWSTR), ("desktop", w.LPWSTR), ("title", w.LPWSTR),
                    *[(name, w.DWORD) for name in ("x", "y", "xsize", "ysize", "xchars", "ychars", "fill", "flags")],
                    ("show", w.WORD), ("reservedsize", w.WORD), ("reserved2", c.c_void_p),
                    ("stdin", w.HANDLE), ("stdout", w.HANDLE), ("stderr", w.HANDLE)]
    class SIX(c.Structure):
        _fields_ = [("info", SI), ("attributes", c.c_void_p)]
    class PI(c.Structure):
        _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", w.DWORD), ("tid", w.DWORD)]
    class CAP(c.Structure):
        _fields_ = [("sid", c.c_void_p), ("capabilities", c.c_void_p), ("count", w.DWORD), ("reserved", w.DWORD)]
    signatures = {
        "CreatePipe": ([c.POINTER(w.HANDLE), c.POINTER(w.HANDLE), c.POINTER(SA), w.DWORD], w.BOOL),
        "SetHandleInformation": ([w.HANDLE, w.DWORD, w.DWORD], w.BOOL), "CloseHandle": ([w.HANDLE], w.BOOL),
        "CreateFileW": ([w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(SA), w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
        "InitializeProcThreadAttributeList": ([c.c_void_p, w.DWORD, w.DWORD, c.POINTER(c.c_size_t)], w.BOOL),
        "UpdateProcThreadAttribute": ([c.c_void_p, w.DWORD, c.c_size_t, c.c_void_p, c.c_size_t, c.c_void_p, c.c_void_p], w.BOOL),
        "DeleteProcThreadAttributeList": ([c.c_void_p], None),
        "CreateProcessW": ([w.LPCWSTR, w.LPWSTR, c.c_void_p, c.c_void_p, w.BOOL, w.DWORD, c.c_void_p, w.LPCWSTR, c.POINTER(SIX), c.POINTER(PI)], w.BOOL),
        "ResumeThread": ([w.HANDLE], w.DWORD), "GetExitCodeProcess": ([w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
        "WaitForSingleObject": ([w.HANDLE, w.DWORD], w.DWORD), "TerminateProcess": ([w.HANDLE, w.UINT], w.BOOL),
        "LocalFree": ([c.c_void_p], c.c_void_p),
    }
    for name, (args, ret) in signatures.items():
        getattr(k, name).argtypes, getattr(k, name).restype = args, ret
    u.CreateAppContainerProfile.argtypes = [w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, c.c_void_p, w.DWORD, c.POINTER(c.c_void_p)]
    u.CreateAppContainerProfile.restype = c.c_long
    u.DeleteAppContainerProfile.argtypes, u.DeleteAppContainerProfile.restype = [w.LPCWSTR], c.c_long
    a.ConvertSidToStringSidW.argtypes, a.ConvertSidToStringSidW.restype = [c.c_void_p, c.POINTER(w.LPWSTR)], w.BOOL
    a.FreeSid.argtypes, a.FreeSid.restype = [c.c_void_p], c.c_void_p
    return c, w, k, u, a, SA, SIX, PI, CAP


def _acl(path, sid, *, remove=False, write=False):
    exe = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe")
    args = [exe, str(path), "/remove:g" if remove else "/grant", "*" + sid + ("" if remove else ":(OI)(CI)" + ("M" if write else "RX")), "/Q"]
    done = subprocess.run(args, capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
    if done.returncode:
        raise FileError("SANDBOX_ACL_FAILED", "沙箱自有目录授权失败；未回退本机权限：" + done.stderr.decode("utf-8", "replace")[-500:])


class NativeProcess:
    def __init__(self, argv, cwd, environment, workspace, memory_mb):
        import msvcrt
        c, w, k, u, a, SA, SIX, PI, CAP = _api()
        self.kernel, self.userenv, self.advapi = k, u, a
        self._handle, self.returncode, self.stdout, self.stderr, self.stdin = None, None, None, None, None
        self.sandbox_job, self.sid, self.profile = None, c.c_void_p(), "ChatCode." + uuid.uuid4().hex
        self.grants, self.lock = [], threading.RLock()
        handles, attrs, pi = [], None, PI()
        try:
            hr = u.CreateAppContainerProfile(self.profile, self.profile, "ChatCode isolated command", None, 0, c.byref(self.sid))
            if hr:
                raise FileError("SANDBOX_UNAVAILABLE", f"AppContainer 创建失败 HRESULT=0x{hr & 0xffffffff:08x}")
            text = w.LPWSTR()
            if not a.ConvertSidToStringSidW(self.sid, c.byref(text)):
                raise c.WinError(c.get_last_error())
            self.sid_text = text.value
            k.LocalFree(c.cast(text, c.c_void_p))
            for path, write in ((workspace, True), (runtime_dir(), False)):
                tree_usage(path)
                _acl(path, self.sid_text, write=write)
                self.grants.append(path)
            sa = SA(c.sizeof(SA), None, True)
            output, output_write, error, error_write = w.HANDLE(), w.HANDLE(), w.HANDLE(), w.HANDLE()
            for read, write in ((output, output_write), (error, error_write)):
                if not k.CreatePipe(c.byref(read), c.byref(write), c.byref(sa), 0):
                    raise c.WinError(c.get_last_error())
                handles.extend([read.value, write.value])
                if not k.SetHandleInformation(read, 1, 0):
                    raise c.WinError(c.get_last_error())
            nul = k.CreateFileW("NUL", 0x80000000, 3, c.byref(sa), 3, 0, None)
            if nul == c.c_void_p(-1).value:
                raise c.WinError(c.get_last_error())
            handles.append(nul)
            size = c.c_size_t()
            k.InitializeProcThreadAttributeList(None, 2, 0, c.byref(size))
            attrs = c.create_string_buffer(size.value)
            if not k.InitializeProcThreadAttributeList(attrs, 2, 0, c.byref(size)):
                raise c.WinError(c.get_last_error())
            # Keep both the SID and its array alive until CreateProcessW returns.
            # SE_GROUP_ENABLED and WinCapabilityInternetClientSid grant public outbound access.
            class SidAndAttributes(c.Structure):
                _fields_ = [("sid", c.c_void_p), ("attributes", w.DWORD)]
            a.CreateWellKnownSid.argtypes = [c.c_int, c.c_void_p, c.c_void_p, c.POINTER(w.DWORD)]
            a.CreateWellKnownSid.restype = w.BOOL
            internet_size = w.DWORD(68)  # SECURITY_MAX_SID_SIZE
            internet_sid = c.create_string_buffer(internet_size.value)
            if not a.CreateWellKnownSid(85, None, internet_sid, c.byref(internet_size)):
                raise FileError("SANDBOX_NETWORK_UNAVAILABLE", "无法创建沙箱联网能力；未回退宿主权限")
            internet_entries = (SidAndAttributes * 1)(SidAndAttributes(c.cast(internet_sid, c.c_void_p), 4))
            capabilities = CAP(self.sid, c.cast(internet_entries, c.c_void_p), 1, 0)
            inherited = (w.HANDLE * 3)(nul, output_write.value, error_write.value)
            for kind, value in ((0x20009, capabilities), (0x20002, inherited)):
                if not k.UpdateProcThreadAttribute(attrs, 0, kind, c.byref(value), c.sizeof(value), None, None):
                    raise c.WinError(c.get_last_error())
            startup = SIX()
            startup.info.cb, startup.info.flags = c.sizeof(SIX), 0x100
            startup.info.stdin, startup.info.stdout, startup.info.stderr = nul, output_write.value, error_write.value
            startup.attributes = c.cast(attrs, c.c_void_p)
            env = c.create_unicode_buffer("\0".join(key + "=" + value for key, value in sorted(environment.items(), key=lambda item: item[0].upper())) + "\0\0")
            command = c.create_unicode_buffer(subprocess.list2cmdline(argv))
            if not k.CreateProcessW(argv[0], command, None, None, True, 0x80000 | 0x400 | 0x4 | 0x08000000, env, str(cwd), c.byref(startup), c.byref(pi)):
                raise c.WinError(c.get_last_error())
            self._handle, self.pid = pi.process, pi.pid
            from .processes import _WindowsJob
            self.sandbox_job = _WindowsJob(self, memory_mb=memory_mb, process_limit=64, cpu_rate=2000)
            for name, handle in (("stdout", output.value), ("stderr", error.value)):
                fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
                handles.remove(handle)
                setattr(self, name, os.fdopen(fd, "rb", buffering=0))
            if k.ResumeThread(pi.thread) == 0xffffffff:
                raise c.WinError(c.get_last_error())
        except BaseException:
            if self._handle:
                k.TerminateProcess(self._handle, 1)
                k.WaitForSingleObject(self._handle, 5000)
            self.cleanup()
            for stream in (self.stdout, self.stderr):
                if stream is not None:
                    stream.close()
            raise
        finally:
            if pi.thread:
                k.CloseHandle(pi.thread)
            for handle in handles:
                k.CloseHandle(handle)
            if attrs is not None:
                k.DeleteProcThreadAttributeList(attrs)

    def poll(self):
        import ctypes as c
        from ctypes import wintypes as w
        if self.returncode is None and self.kernel.WaitForSingleObject(self._handle, 0) == 0:
            code = w.DWORD()
            self.kernel.GetExitCodeProcess(self._handle, c.byref(code))
            self.returncode = code.value
        return self.returncode

    def wait(self, timeout=None):
        if self.kernel.WaitForSingleObject(self._handle, 0xffffffff if timeout is None else min(int(timeout * 1000), 0xfffffffe)) == 0x102:
            raise subprocess.TimeoutExpired("AppContainer command", timeout)
        return self.poll()

    def kill(self):
        self.kernel.TerminateProcess(self._handle, 1)

    def cleanup(self):
        with self.lock:
            if self.sandbox_job:
                self.sandbox_job.close()
            failures = []
            for path in self.grants:
                try:
                    _acl(path, self.sid_text, remove=True)
                except (FileError, OSError, subprocess.TimeoutExpired) as exc:
                    failures.append(str(exc))
            self.grants = []
            if self.sid:
                hr = self.userenv.DeleteAppContainerProfile(self.profile)
                if hr:
                    failures.append(f"AppContainer 配置回收失败：0x{hr & 0xffffffff:08x}")
                self.advapi.FreeSid(self.sid)
                self.sid = None
            if self._handle:
                self.kernel.CloseHandle(self._handle)
                self._handle = None
            if failures:
                raise FileError("SANDBOX_CLEANUP_FAILED", "; ".join(failures)[:1000])


def launch(workspace, settings, *, command, argv, cwd, shell, env, timeout):
    _api()
    home = getattr(workspace, "_isolation_home", None)
    if home is None:
        raise FileError("SANDBOX_COPY_REQUIRED", "原生沙箱只在独立任务副本中执行；不会授权原项目")
    if timeout == 0:
        raise FileError("SANDBOX_TIMEOUT_REQUIRED", "沙箱命令必须设置总超时")
    if not 128 <= settings.sandbox_memory_mb <= 8192:
        raise FileError("SANDBOX_BAD_CONFIG", "沙箱内存范围为 128..8192 MiB")
    task_id = workspace.root.parent.name
    expected = home.isolated.directory(task_id) / "work"
    if workspace.root != expected or workspace.root.resolve() != expected or home.isolated._link(expected):
        raise FileError("SANDBOX_UNSAFE_PATH", "任务执行目录不是本服务创建的普通副本目录")
    manager = workspace._processes
    if manager and any(not job["done"].is_set() for job in manager.jobs.values()):
        raise FileError("SANDBOX_BUSY", "同一副本的原生沙箱命令顺序执行，以免授权期间目录被其他进程改动")
    check_quota(home, settings.storage_quota_mb)
    tree_usage(workspace.root)
    runtime = runtime_dir()
    if not (runtime / "python.exe").is_file():
        raise FileError("SANDBOX_RUNTIME_MISSING", "发行包缺少内置 Python 沙箱运行时")
    if argv:
        run = list(argv)
        run[0] = str(runtime / "python.exe") if Path(argv[0]).name.lower() in ("python", "python.exe", "python3", "python3.exe") else shutil.which(argv[0]) or argv[0]
    elif shell == "powershell":
        run = [shutil.which("powershell") or "powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command]
    elif shell == "bash":
        raise FileError("SANDBOX_SHELL_UNSUPPORTED", "内置 Windows 沙箱支持 cmd/PowerShell 或 argv")
    else:
        run = [str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "cmd.exe"), "/d", "/s", "/c", command]
    tmp = workspace.root / ".chatcode-tmp"
    tmp.mkdir(exist_ok=True)
    windows = os.environ.get("SystemRoot", r"C:\Windows")
    environment = {"SystemRoot": windows, "WINDIR": windows, "PATH": str(runtime) + os.pathsep + str(Path(windows) / "System32"),
        "TEMP": str(tmp), "TMP": str(tmp), "HOME": str(tmp), "USERPROFILE": str(tmp),
        "SystemDrive": Path(windows).drive, "APPDATA": str(tmp), "LOCALAPPDATA": str(tmp),
        "ProgramData": os.environ.get("ProgramData", r"C:\ProgramData"), "ALLUSERSPROFILE": os.environ.get("ProgramData", r"C:\ProgramData"),
        "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    for key, value in (env or {}).items():
        if not key or "=" in key or "\0" in key + value or len(value) > 16000:
            raise FileError("INVALID_ARGUMENT", "环境变量名称或值无效")
        environment[key] = value
    process = NativeProcess(run, cwd, environment, workspace.root, settings.sandbox_memory_mb)
    metadata = {"mode": "native", "os_isolation": "windows_appcontainer", **network_policy(),
        "filesystem": "task_copy_modify; runtime_read_execute; Windows_public_resources", "host_private_profile_granted": False,
        "host_environment_inherited": False, "memory_mb": settings.sandbox_memory_mb, "active_process_limit": 64,
        "cpu_rate_percent": 20, "fallback_to_host": False, "requires_external_sandbox_software": False}
    return process, metadata


def status(ctx, args):
    from .tool_types import ToolResult
    data = {"mode": ctx.settings.execution_mode, "available": False, "fallback_to_host": False,
        "backend": "windows_appcontainer", "requires_external_sandbox_software": False,
        "runtime_included": (runtime_dir() / "python.exe").is_file(),
        **network_policy(),
        "notice": "允许公网出站，保留私有文件、进程和资源隔离；回环访问遵循 Windows AppContainer 策略。Windows 公开只读资源仍可能可读，不是虚拟机。"}
    try:
        _api()
        data["available"] = data["runtime_included"]
    except (FileError, OSError, AttributeError) as exc:
        data.update(code=getattr(exc, "code", "SANDBOX_UNAVAILABLE"), message=str(exc))
    return ToolResult(json.dumps(data, ensure_ascii=False), data)
