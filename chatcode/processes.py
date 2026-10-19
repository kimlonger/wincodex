"""有界日志与后台进程管理；任务只允许由创建它的服务实例停止。"""

from __future__ import annotations

import codecs
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid

from .config import app_data_dir
from .files import FileError, atomic_write_bytes, bytes_revision


class _WindowsJob:
    def __init__(self, process, *, memory_mb=None, process_limit=None, cpu_rate=None):
        import ctypes as c
        from ctypes import wintypes as w

        class Basic(c.Structure):
            _fields_ = [("ProcessTime", c.c_longlong), ("JobTime", c.c_longlong), ("Flags", w.DWORD),
                        ("MinWorkingSet", c.c_size_t), ("MaxWorkingSet", c.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", c.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes", "OtherBytes")]
        class Extended(c.Structure):
            _fields_ = [("Basic", Basic), ("IO", IO), ("ProcessMemory", c.c_size_t), ("JobMemory", c.c_size_t),
                        ("PeakProcessMemory", c.c_size_t), ("PeakJobMemory", c.c_size_t)]
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [c.c_void_p, w.LPCWSTR], w.HANDLE
        kernel.SetInformationJobObject.argtypes, kernel.SetInformationJobObject.restype = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL
        kernel.AssignProcessToJobObject.argtypes, kernel.AssignProcessToJobObject.restype = [w.HANDLE, w.HANDLE], w.BOOL
        kernel.TerminateJobObject.argtypes, kernel.TerminateJobObject.restype = [w.HANDLE, w.UINT], w.BOOL
        kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_mb is not None:
            info.Basic.Flags |= 0x200
            info.JobMemory = memory_mb * 1024 * 1024
        if process_limit is not None:
            info.Basic.Flags |= 0x8
            info.Basic.ActiveProcessLimit = process_limit
        if not handle or not kernel.SetInformationJobObject(handle, 9, c.byref(info), c.sizeof(info)) or not kernel.AssignProcessToJobObject(handle, int(process._handle)):
            if handle:
                kernel.CloseHandle(handle)
            raise OSError(c.get_last_error(), "无法建立 Windows 进程 Job")
        self.kernel, self.handle = kernel, handle
        self.lock = threading.Lock()
        if cpu_rate is not None:
            class CPU(c.Structure):
                _fields_ = [("flags", w.DWORD), ("rate", w.DWORD)]
            rate = CPU(1 | 4, cpu_rate)
            if not kernel.SetInformationJobObject(handle, 15, c.byref(rate), c.sizeof(rate)):
                self.close()
                raise OSError(c.get_last_error(), "无法设置沙箱 CPU 限额")

    def close(self):
        with self.lock:
            if self.handle:
                self.kernel.TerminateJobObject(self.handle, 1)
                self.kernel.CloseHandle(self.handle)
                self.handle = None


class _Log:
    LIMIT = 4 * 1024 * 1024

    def __init__(self, path):
        self.path, self.base, self.total = path, 0, 0
        self.lock = threading.Lock()
        path.write_bytes(b"")

    def append(self, data):
        with self.lock:
            with self.path.open("ab") as handle:
                handle.write(data)
            self.total += len(data)
            size = self.total - self.base
            if size > self.LIMIT:
                keep = self.LIMIT // 2
                with self.path.open("rb") as handle:
                    handle.seek(-keep, os.SEEK_END)
                    tail = handle.read(keep)
                self.path.write_bytes(tail)
                self.base = self.total - keep

    def read(self, offset, budget, finished):
        with self.lock:
            if offset > self.total:
                raise FileError("INVALID_CURSOR", "日志游标超过已生成内容")
            start = max(offset, self.base)
            with self.path.open("rb") as handle:
                handle.seek(start - self.base)
                data = handle.read(budget)
            encoding = "utf-8"
            try:
                decoder = codecs.getincrementaldecoder("utf-8")("strict")
                text = decoder.decode(data, final=finished and start + len(data) == self.total)
            except UnicodeError:
                encoding = "gb18030"
                decoder = codecs.getincrementaldecoder(encoding)("replace")
                text = decoder.decode(data, final=finished and start + len(data) == self.total)
            pending = decoder.getstate()[0]
            return text, start + len(data) - len(pending), start - offset, encoding


class ProcessManager:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = app_data_dir() / "processes" / bytes_revision(os.path.normcase(str(workspace.root)).encode())[:24]
        self.jobs = {}
        self.lock = threading.RLock()
        self.closed = False

    def _kill(self, job):
        process = job["process"]
        if job.get("windows_job"):
            job["windows_job"].close()
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=8,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            finally:
                if process.poll() is None:
                    process.kill()

    def _prune_disk(self):
        completed = []
        if not self.store.exists():
            return
        for directory in self.store.iterdir():
            if len(directory.name) != 32 or directory.is_symlink() or not directory.is_dir():
                continue
            try:
                data = json.loads((directory / "process.json").read_text(encoding="utf-8"))
                if data.get("state") in ("exited", "timed_out", "cancelled", "failed"):
                    completed.append((data.get("finished_at") or 0, directory))
            except (OSError, ValueError, AttributeError):
                continue
        for _stamp, directory in sorted(completed, reverse=True)[32:]:
            if directory.resolve().is_relative_to(self.store.resolve()):
                shutil.rmtree(directory)

    def _summary(self, job):
        result = {key: job.get(key) for key in ("process_id", "command", "cwd", "state", "exit_code", "timed_out",
                                              "started_at", "finished_at", "tree_control", "log_error", "check_name", "task_id", "validation_warning", "task_tracking_warning", "workspace_revision", "sandbox")}
        result["command_truncated"] = len(result["command"]) > 2000
        result["command"] = result["command"][:2000]
        return result

    def _save(self, job):
        metadata = self._summary(job)
        metadata.update(stdout_base=job["stdout_log"].base, stderr_base=job["stderr_log"].base)
        atomic_write_bytes(job["directory"] / "process.json", json.dumps(metadata, ensure_ascii=False).encode(), mode=0o600)
        if job.get("task_id"):
            try:
                self.workspace.tasks.process_update(metadata)
            except Exception as exc:
                job["task_tracking_warning"] = str(exc)[:500]

    def _pump(self, job, stream, log):
        try:
            while data := stream.read(8192):
                try:
                    log.append(data)
                except OSError as exc:
                    job["log_error"] = str(exc)
        finally:
            stream.close()

    def _monitor(self, job, timeout):
        try:
            try:
                job["process"].wait(timeout=timeout or None)
            except subprocess.TimeoutExpired:
                job["timed_out"] = True
            finally:
                # 父命令结束时一并清理其后台后代，防止后代持有日志管道。
                self._kill(job)
                job["process"].wait(timeout=8)
            for thread in job["readers"]:
                thread.join(timeout=3)
            job["exit_code"] = job["process"].returncode
            job["state"] = "cancelled" if job["cancelled"] else "timed_out" if job["timed_out"] else "exited"
        except Exception as exc:
            job["state"], job["log_error"] = "failed", str(exc)
        finally:
            job["finished_at"] = time.time()
            if hasattr(job["process"], "cleanup"):
                try:
                    job["process"].cleanup()
                except Exception as exc:
                    job["state"], job["log_error"] = "failed", str(exc)
            if job.get("check_name"):
                tail = ""
                for name in ("stdout.log", "stderr.log"):
                    try:
                        with (job["directory"] / name).open("rb") as handle:
                            handle.seek(max(0, (job["directory"] / name).stat().st_size - 64_000))
                            tail += handle.read(64_000).decode("utf-8", errors="replace")
                    except OSError:
                        pass
                if re.search(r"Ran 0 tests\b|no tests ran\b", tail):
                    job["validation_warning"] = "检查没有发现测试，请核对配置"
                home = getattr(self.workspace, "_isolation_home", None)
                if home is not None:
                    try:
                        job["workspace_revision"] = home._isolated.tree_revision(job["task_id"])
                    except Exception as exc:
                        job["validation_warning"] = f"检查结束后无法核对任务副本：{exc}"
            try:
                self._save(job)
            except OSError as exc:
                job["log_error"] = str(exc)
            job["done"].set()

    def start(self, *, command=None, argv=None, cwd=None, shell="default", env=None, timeout=60, yield_ms=1000,
              operation_id=None, check_name=None, task_id=None, settings=None):
        home = getattr(self.workspace, "_isolation_home", self.workspace)
        with home.write_lock:
            return self._start_request(command=command, argv=argv, cwd=cwd, shell=shell, env=env, timeout=timeout,
                yield_ms=yield_ms, operation_id=operation_id, check_name=check_name, task_id=task_id, settings=settings)

    def _start_request(self, *, command=None, argv=None, cwd=None, shell="default", env=None, timeout=60, yield_ms=1000,
                       operation_id=None, check_name=None, task_id=None, settings=None):
        request = dict(command=command, argv=argv, cwd=cwd, shell=shell, env=env, timeout=timeout, check_name=check_name, task_id=task_id)
        if not operation_id:
            return self._start(**request, yield_ms=yield_ms, settings=settings)
        # 先持久化预约，再启动进程。启动中崩溃的请求状态不明，拒绝自动重跑。
        fingerprint = bytes_revision(json.dumps({**request, "execution_mode": getattr(settings, "execution_mode", "host")}, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        directory = self.store / "operations"
        record_path = directory / (bytes_revision(operation_id.encode("utf-8")) + ".json")
        replayed = False
        with self.workspace.write_lock:
            directory.mkdir(parents=True, exist_ok=True)
            if record_path.exists():
                try:
                    record = json.loads(record_path.read_text(encoding="utf-8"))
                    if not isinstance(record, dict):
                        raise ValueError("invalid operation record")
                except (ValueError, OSError):
                    raise FileError("OPERATION_UNAVAILABLE", "命令去重记录无法读取，请核对历史，不能自动重复执行") from None
                if record.get("fingerprint") != fingerprint:
                    raise FileError("OPERATION_ID_REUSED", "operation_id 已用于不同命令或检查，请使用新的 ID")
                identity = record.get("process_id")
                with self.lock:
                    available = identity in self.jobs
                if not available:
                    raise FileError("OPERATION_UNAVAILABLE", "此命令已经预约或执行，但不在当前服务的任务中；请核对磁盘日志，不能自动重跑",
                                    {"process_id": identity, "state": record.get("state")})
                replayed = True
            else:
                kept = 0
                for path in directory.glob("*.json"):
                    if path.is_symlink():
                        continue
                    if path.stat().st_mtime < time.time() - 30 * 86_400:
                        path.unlink()
                    else:
                        kept += 1
                if kept >= 10_000:
                    raise FileError("OPERATION_LIMIT", "当前工作区的命令去重记录已达 10000 条上限，未启动命令")
                record = {"fingerprint": fingerprint, "state": "reserved", "created_at": time.time()}
                atomic_write_bytes(record_path, json.dumps(record).encode(), mode=0o600)
                try:
                    started = self._start(**request, yield_ms=0, settings=settings)
                    identity = started["process_id"]
                    record.update(process_id=identity, state="started")
                except Exception:
                    record["state"] = "launch_failed_or_unknown"
                    atomic_write_bytes(record_path, json.dumps(record).encode(), mode=0o600)
                    raise
                atomic_write_bytes(record_path, json.dumps(record).encode(), mode=0o600)
        result = self.read(identity, wait_ms=yield_ms)
        result.update(operation_id=operation_id, replayed=replayed)
        return result

    def _start(self, *, command=None, argv=None, cwd=None, shell="default", env=None, timeout=60, yield_ms=1000,
               check_name=None, task_id=None, settings=None):
        home = getattr(self.workspace, "_isolation_home", self.workspace)
        with home._process_lock:
            group = [home._processes]
            if home._isolated is not None:
                group.extend(copy._processes for copy in home._isolated.workspaces.values())
        active = 0
        for manager in group:
            if manager is not None:
                with manager.lock:
                    active += sum(not job["done"].is_set() for job in manager.jobs.values())
        if active >= 4:
            raise FileError("PROCESS_LIMIT", "原项目和所有任务副本合计最多同时运行 4 个命令")
        if bool(command) == bool(argv):
            raise FileError("INVALID_ARGUMENT", "command 和 argv 必须提供且只提供一种")
        target = self.workspace.resolve_existing(cwd or ".").absolute
        if not target.is_dir():
            raise FileError("NOT_A_DIRECTORY", "cwd 必须是工作区内的目录")
        use_shell = False
        sandbox_metadata = {"mode": "host", "os_isolation": False}
        native_mode = settings is not None and settings.execution_mode == "native"
        if settings and getattr(self.workspace, "_isolation_home", None):
            from .maintenance import check_quota
            check_quota(home, settings.storage_quota_mb)
        if native_mode:
            launch, display = argv or command, command or " ".join(argv)
        elif argv:
            launch = argv
            display = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
        elif shell == "powershell":
            executable = shutil.which("pwsh") or shutil.which("powershell")
            if not executable:
                raise FileError("SHELL_MISSING", "找不到 PowerShell")
            launch = [executable, "-NoProfile", "-NonInteractive", "-Command",
                      "$OutputEncoding = [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding; " + command]
            display = command
        elif shell == "bash":
            executable = shutil.which("bash")
            if not executable:
                raise FileError("SHELL_MISSING", "找不到 Bash")
            launch, display = [executable, "--noprofile", "--norc", "-c", command], command
        else:
            launch, display, use_shell = command, command, True
        environment = os.environ.copy()
        environment.setdefault("PYTHONIOENCODING", "utf-8")
        environment.setdefault("PYTHONUNBUFFERED", "1")
        environment.update(env or {})
        with self.lock:
            if self.closed:
                raise FileError("SERVICE_STOPPING", "服务正在停止，无法创建任务")
            if sum(not job["done"].is_set() for job in self.jobs.values()) >= 4:
                raise FileError("PROCESS_LIMIT", "最多同时运行 4 个任务，请等待或停止已有任务")
            completed = [job for job in self.jobs.values() if job["done"].is_set()]
            for old in sorted(completed, key=lambda j: j["started_at"])[:-31]:
                self.jobs.pop(old["process_id"], None)
            try:
                self._prune_disk()
            except OSError:
                pass
            identity = uuid.uuid4().hex
            directory = self.store / identity
            directory.mkdir(parents=True)
            stdout_log, stderr_log = _Log(directory / "stdout.log"), _Log(directory / "stderr.log")
            options = {"shell": use_shell, "cwd": str(target), "env": environment, "stdin": subprocess.DEVNULL,
                       "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "bufsize": 0}
            if os.name == "nt":
                options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if shell == "cmd" or (use_shell and shell == "default"):
                    options["executable"] = os.environ.get("COMSPEC", "cmd.exe")
            else:
                options["start_new_session"] = True
            try:
                if native_mode:
                    from .sandbox import launch as sandbox_launch
                    process, sandbox_metadata = sandbox_launch(self.workspace, settings,
                        command=command, argv=argv, cwd=target, shell=shell, env=env, timeout=timeout)
                else:
                    process = subprocess.Popen(launch, **options)
            except (OSError, FileError) as exc:
                for name in ("stdout.log", "stderr.log"):
                    (directory / name).unlink(missing_ok=True)
                directory.rmdir()
                if isinstance(exc, FileError):
                    raise
                raise FileError("COMMAND_FAILED", str(exc)) from exc
            job = {"process_id": identity, "process": process, "command": display, "cwd": str(target), "state": "running",
                   "check_name": check_name, "task_id": task_id,
                   "exit_code": None, "timed_out": False, "cancelled": False, "started_at": time.time(), "done": threading.Event(),
                   "directory": directory, "stdout_log": stdout_log, "stderr_log": stderr_log, "windows_job": None,
                   "sandbox": sandbox_metadata,
                   "tree_control": "process_group" if os.name != "nt" else "taskkill"}
            if os.name == "nt":
                try:
                    job["windows_job"] = process.sandbox_job if native_mode else _WindowsJob(process)
                    job["tree_control"] = "windows_job"
                except OSError:
                    pass
            readers = [threading.Thread(target=self._pump, args=(job, process.stdout, stdout_log), daemon=True),
                       threading.Thread(target=self._pump, args=(job, process.stderr, stderr_log), daemon=True)]
            job["readers"] = readers
            self.jobs[identity] = job
            try:
                self._save(job)
            except OSError:
                self._kill(job)
                process.wait(timeout=8)
                process.stdout.close()
                process.stderr.close()
                self.jobs.pop(identity, None)
                raise
            for reader in readers:
                reader.start()
            threading.Thread(target=self._monitor, args=(job, timeout), daemon=True).start()
        job["done"].wait(yield_ms / 1000)
        return self.read(identity)

    def read(self, identity, *, stdout_offset=0, stderr_offset=0, max_bytes=32_000, wait_ms=0):
        with self.lock:
            job = self.jobs.get(identity)
        if job is None:
            raise FileError("PROCESS_NOT_FOUND", "当前服务中找不到该任务，可能已重启或超过保留数量")
        job["done"].wait(wait_ms / 1000)
        finished = job["done"].is_set()
        out, next_out, lost_out, out_encoding = job["stdout_log"].read(stdout_offset, max_bytes // 2, finished)
        err, next_err, lost_err, err_encoding = job["stderr_log"].read(stderr_offset, max_bytes // 2, finished)
        return {**self._summary(job), "running": not finished, "stdout": out, "stderr": err,
                "next_stdout_offset": next_out, "next_stderr_offset": next_err,
                "stdout_lost_bytes": lost_out, "stderr_lost_bytes": lost_err,
                "stdout_encoding": out_encoding, "stderr_encoding": err_encoding,
                "duration_seconds": round((job.get("finished_at") or time.time()) - job["started_at"], 3)}

    def stop(self, identity):
        with self.lock:
            job = self.jobs.get(identity)
        if job is None:
            raise FileError("PROCESS_NOT_FOUND", "当前服务中找不到该任务")
        if not job["done"].is_set():
            job["cancelled"] = True
            self._kill(job)
            job["done"].wait(10)
        return self.read(identity)

    def summaries(self):
        with self.lock:
            return [self._summary(job) for job in sorted(self.jobs.values(), key=lambda j: j["started_at"], reverse=True)]

    def shutdown(self):
        with self.lock:
            self.closed = True
            identities = [key for key, job in self.jobs.items() if not job["done"].is_set()]
        for identity in identities:
            self.stop(identity)
