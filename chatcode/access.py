"""启动时固定的文件访问策略；命令与语言服务进程不属于文件沙箱。"""

import fnmatch
import os
from pathlib import PurePosixPath


class FilePolicy:
    def __init__(self, root, *, allow_sensitive=False):
        self.allow_sensitive = allow_sensitive
        self.patterns = []
        control = root / ".chatcodeignore"
        if control.exists() or control.is_symlink():
            if control.is_symlink() or not control.resolve().is_relative_to(root):
                raise ValueError(".chatcodeignore 必须是工作区内的普通文件")
            with control.open("rb") as handle:
                raw = handle.read(64_001)
            if len(raw) > 64_000:
                raise ValueError(".chatcodeignore 超过 64 KB")
            for line in raw.decode("utf-8-sig").splitlines():
                pattern = line.strip().replace("\\", "/")
                if not pattern or pattern.startswith("#"):
                    continue
                if pattern.startswith("!") or ".." in pattern.split("/"):
                    raise ValueError(".chatcodeignore 仅支持拒绝规则，不支持 ! 或 ..")
                self.patterns.append(pattern.lstrip("/"))

    def blocked(self, relative):
        path = PurePosixPath(relative.casefold() if os.name == "nt" else relative)
        if not self.allow_sensitive:
            for part in path.parts:
                name = part.lower()
                example = name.endswith((".example", ".sample", ".template"))
                if ((name == ".env" or name.startswith(".env.")) and not example
                        or name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
                        or name.endswith((".pem", ".key", ".p12", ".pfx"))):
                    return True
        value = path.as_posix()
        for pattern in self.patterns:
            if os.name == "nt":
                pattern = pattern.casefold()
            if pattern.endswith("/"):
                directory = pattern.rstrip("/")
                if "/" not in directory:
                    if any(fnmatch.fnmatchcase(part, directory) for part in path.parts):
                        return True
                elif value == directory or value.startswith(directory + "/"):
                    return True
            elif "/" not in pattern:
                if any(fnmatch.fnmatchcase(part, pattern) for part in path.parts):
                    return True
            elif fnmatch.fnmatchcase(value, pattern):
                return True
        return False
