"""同一工作区的可重入跨线程/跨进程写锁，锁文件位于应用数据目录。"""

from __future__ import annotations

import os
from pathlib import Path
import threading
import time

from .config import app_data_dir
from .files import FileError, bytes_revision


class WorkspaceLock:
    def __init__(self, root: Path) -> None:
        key = bytes_revision(os.path.normcase(str(root)).encode("utf-8"))[:24]
        self.path = app_data_dir() / "locks" / f"{key}.lock"
        self._thread_lock = threading.RLock()
        self._local = threading.local()

    def __enter__(self):
        self._thread_lock.acquire()
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            return self
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+b")
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                deadline = time.monotonic() + 30
                while True:
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise FileError("WORKSPACE_BUSY", "工作区正在被另一进程修改，请稍后重试") from None
                        time.sleep(0.05)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self._local.handle = handle
            self._local.depth = 1
            return self
        except BaseException:
            if handle is not None:
                handle.close()
            self._thread_lock.release()
            raise

    def __exit__(self, *args):
        self._local.depth -= 1
        try:
            if self._local.depth == 0:
                handle = self._local.handle
                try:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
                    del self._local.handle
        finally:
            self._thread_lock.release()
