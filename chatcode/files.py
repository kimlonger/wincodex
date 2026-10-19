"""文件读写原语。

所有写操作都走"同目录临时文件 + fsync + 原子替换"，避免写到一半崩掉留下半个文件。
revision 是内容哈希，用来做乐观锁：模型读到的是 v1，改的时候带上 expected_revision=v1，
若期间文件被别人（或另一次对话）改过就拒绝，防止静默覆盖。
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


class FileError(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class FileContent:
    text: str
    encoding: str
    newline: str      # "\n" / "\r\n" / "\r"
    has_bom: bool
    revision: str
    line_count: int
    size_bytes: int
    raw: bytes = b""


def content_revision(text: str) -> str:
    """默认 UTF-8 文本的版本；磁盘文件使用 bytes_revision，包含编码/BOM/换行。"""
    return bytes_revision(text.encode("utf-8"))


def bytes_revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def encode_text(text: str, *, newline: str = "\n", has_bom: bool = False, encoding: str = "utf-8") -> bytes:
    if newline not in ("\n", "\r\n", "\r"):
        raise FileError("INVALID_NEWLINE", "不支持的换行风格")
    try:
        data = normalize_newlines(text).replace("\n", newline).encode(encoding, errors="strict")
    except (UnicodeError, LookupError) as exc:
        raise FileError("ENCODING_ERROR", f"内容无法用 {encoding} 编码，请显式转换编码后重试") from exc
    return (b"\xef\xbb\xbf" if has_bom and encoding == "utf-8" else b"") + data


def _detect_newline(raw: bytes) -> str:
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    cr = raw.count(b"\r") - crlf
    if crlf >= lf and crlf >= cr and crlf > 0:
        return "\r\n"
    if cr > lf and cr > 0:
        return "\r"
    return "\n"


def read_text_file(path: Path, *, max_bytes: int = 20 * 1024 * 1024) -> FileContent:
    """读文本文件，保留 BOM 与换行风格的信息，方便原样写回。"""
    try:
        if not path.exists():
            raise FileError("NOT_FOUND", f"文件不存在：{path.name}")
        if path.is_dir():
            raise FileError("IS_DIRECTORY", f"这是一个目录：{path.name}")
        size = path.stat().st_size
        if size > max_bytes:
            raise FileError(
                "TOO_LARGE",
                f"文件过大（{size} 字节），超过 {max_bytes} 字节上限",
                {"size": size},
            )
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        size = len(raw)
        if size > max_bytes:
            raise FileError("TOO_LARGE", f"文件在读取时超过 {max_bytes} 字节上限")
    except FileError:
        raise
    except OSError as exc:
        raise FileError("READ_FAILED", f"读取失败：{exc}") from exc

    has_bom = raw.startswith(b"\xef\xbb\xbf")
    body = raw[3:] if has_bom else raw
    # 二进制文件直接拒绝，避免把乱码塞进对话
    if b"\x00" in body[:8192]:
        raise FileError("BINARY_FILE", f"看起来是二进制文件：{path.name}")

    encoding = "utf-8"
    text: str
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        if has_bom:
            raise FileError("DECODE_FAILED", f"带 UTF-8 BOM 的文件包含无效 UTF-8 字节：{path.name}") from None
        for enc in ("gbk", "latin-1"):
            try:
                text = body.decode(enc)
                encoding = enc
                break
            except UnicodeDecodeError:
                continue
        else:
            raise FileError("DECODE_FAILED", f"无法解码文件（非 UTF-8/GBK）：{path.name}") from None

    text = normalize_newlines(text)
    return FileContent(
        text=text,
        encoding=encoding,
        newline=_detect_newline(body),
        has_bom=has_bom,
        revision=bytes_revision(raw),
        line_count=text.count("\n") + (0 if text.endswith("\n") or not text else 1),
        size_bytes=size,
        raw=raw,
    )


def atomic_write(path: Path, text: str, *, newline: str = "\n", has_bom: bool = False, encoding: str = "utf-8") -> None:
    """原子写文件：同目录临时文件 -> fsync -> os.replace。

    临时文件必须和目标同目录，否则跨设备 os.replace 会失败。
    """
    data = encode_text(text, newline=newline, has_bom=has_bom, encoding=encoding)
    atomic_write_bytes(path, data)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """写入原始字节，并尽量保留已有文件权限。"""
    if mode is None and path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)

    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".chatcode-", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except Exception:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass
        raise


def split_lines(text: str) -> list[str]:
    """返回物理行，不把文件末尾的换行算作额外空行。"""
    normalized = normalize_newlines(text)
    lines = normalized.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def line_ranges(text: str, offset: int, limit: int | None) -> tuple[list[str], int, int]:
    """取 [offset, offset+limit) 行（1-based，闭区间语义：offset 从 1 开始）。

    返回 (选中行, 起始行号, 总行数)。
    """
    lines = split_lines(text)
    total = len(lines)
    start = max(1, offset)
    if limit is None:
        end = total
    else:
        end = min(total, start + max(0, limit) - 1)
    selected = lines[start - 1 : end]
    return selected, start, total


class _HashingReader(io.RawIOBase):
    def __init__(self, raw):
        super().__init__()
        self.raw, self.digest = raw, hashlib.sha256()
        self.size = self.cr = self.lf = self.crlf = 0
        self.last_cr = False

    def readable(self):
        return True

    def readinto(self, buffer):
        count = self.raw.readinto(buffer)
        if count:
            data = bytes(memoryview(buffer)[:count])
            self.digest.update(data)
            self.size += count
            self.cr += data.count(b"\r")
            self.lf += data.count(b"\n")
            self.crlf += data.count(b"\r\n") + int(self.last_cr and data.startswith(b"\n"))
            self.last_cr = data.endswith(b"\r")
        return count

    def close(self):
        self.raw.close()
        super().close()


def read_window(path: Path, *, offset: int = 1, limit: int = 2000, max_chars: int = 60_000) -> dict:
    """流式读取窗口，同时计算整个文件的字节版本；超长物理行仅保留预算内前缀。"""
    if path.is_dir():
        raise FileError("IS_DIRECTORY", f"这是目录：{path.name}")
    try:
        before = path.stat()
        with path.open("rb") as probe:
            sample = probe.read(8192)
    except OSError as exc:
        raise FileError("READ_FAILED", str(exc)) from exc
    if b"\0" in sample:
        raise FileError("BINARY_FILE", f"看起来是二进制文件：{path.name}")
    has_bom = sample.startswith(b"\xef\xbb\xbf")
    encodings = ("utf-8-sig",) if has_bom else ("utf-8", "gbk", "latin-1")
    for encoding in encodings:
        selected, truncated_lines, total, used = [], [], 0, 0
        physical = ""
        had_part = False
        try:
            reader = _HashingReader(path.open("rb"))
            with io.TextIOWrapper(io.BufferedReader(reader), encoding=encoding, newline=None) as stream:
                while True:
                    chunk = stream.readline(8192)
                    if not chunk:
                        if had_part:
                            total += 1
                            if offset <= total < offset + limit:
                                selected.append(physical)
                        break
                    had_part = True
                    line_number = total + 1
                    if offset <= line_number < offset + limit:
                        piece = chunk[:-1] if chunk.endswith("\n") else chunk
                        keep = piece[:max(0, max_chars - used)]
                        physical += keep
                        used += len(keep)
                        if len(keep) < len(piece) and line_number not in truncated_lines:
                            truncated_lines.append(line_number)
                    if chunk.endswith("\n"):
                        total += 1
                        if offset <= total < offset + limit:
                            selected.append(physical)
                        physical, had_part = "", False
                revision, size = reader.digest.hexdigest(), reader.size
                crlf, lf, cr = reader.crlf, reader.lf - reader.crlf, reader.cr - reader.crlf
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise FileError("REVISION_MISMATCH", "文件在读取过程中变化，请重试")
            # 预算已耗尽的尾部空记录不占用返回行号，下一次可从这里继续。
            while selected and not selected[-1] and offset + len(selected) - 1 in truncated_lines:
                selected.pop()
            end = offset + len(selected) - 1
            newline = "\r\n" if crlf > 0 and crlf >= lf and crlf >= cr else "\r" if cr > lf and cr > 0 else "\n"
            return {"revision": revision, "encoding": "utf-8" if has_bom else encoding, "newline": newline,
                    "start_line": offset, "end_line": end if selected else None, "total_lines": total,
                    "content": "\n".join(selected), "lines": selected, "size_bytes": size,
                    "truncated": end < total or bool(truncated_lines),
                    "truncated_lines": truncated_lines, "next_offset": end + 1 if end < total else None}
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise FileError("READ_FAILED", str(exc)) from exc
    raise FileError("DECODE_FAILED", "文件编码无效，无法安全读取")
