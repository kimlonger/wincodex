"""项目发现、代码读取和受限只读批处理。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fnmatch
from functools import lru_cache
import json
from pathlib import PurePosixPath
import tomllib

from .code_index import outline, select_symbol
from .files import FileError
from .tool_types import ToolContext, ToolError, ToolResult, spec
from .workspace import PathRejected


@lru_cache(maxsize=2048)
def matches_glob(path: str, pattern: str) -> bool:
    parts, patterns = tuple(path.split("/")), tuple(pattern.replace("\\", "/").split("/"))
    if len(patterns) == 1:
        return fnmatch.fnmatchcase(parts[-1], pattern)
    @lru_cache(maxsize=None)
    def match(i, j):
        if j == len(patterns):
            return i == len(parts)
        if patterns[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatch.fnmatchcase(parts[i], patterns[j]) and match(i + 1, j + 1)
    return match(0, 0)


def _result(data: dict) -> ToolResult:
    return ToolResult(json.dumps(data, ensure_ascii=False), data)


def find_files(ctx: ToolContext, args: dict) -> ToolResult:
    path = args.get("path", ".")
    pattern = args.get("pattern", "**/*")
    start, limit = args.get("offset", 0), args.get("limit", 100)
    matched = []
    scanned = 0
    for relative, is_dir in ctx.workspace.iter_entries(path, max_entries=50_001):
        scanned += 1
        if not is_dir and matches_glob(relative, pattern):
            matched.append(relative)
        if len(matched) > start + limit:
            break
    selected = matched[start:start + limit]
    more = len(matched) > start + limit
    return _result({"files": selected, "count": len(selected), "next_offset": start + len(selected) if more else None,
                    "truncated": more or scanned >= 50_001, "scan_limit_reached": scanned >= 50_001})


def project_overview(ctx: ToolContext, args: dict) -> ToolResult:
    root = ctx.workspace.root
    entries = list(ctx.workspace.iter_entries(recursive=False, max_entries=101))
    types, sources, checks = [], [], []
    markers = {"pyproject.toml": "python", "requirements.txt": "python", "setup.cfg": "python",
               "package.json": "javascript", "Cargo.toml": "rust", "go.mod": "go", "pom.xml": "java",
               "build.gradle": "java", "build.gradle.kts": "java"}
    for name, kind in markers.items():
        if (root / name).is_file():
            # 显式配置文件同样经过工作区边界检查。
            ctx.workspace.resolve_existing(name)
            sources.append(name)
            if kind not in types:
                types.append(kind)
    if "python" in types or any(name.endswith(".py") for name, is_dir in entries if not is_dir):
        if "python" not in types:
            types.append("python")
        if (root / "tests" / "run.py").is_file():
            checks.append({"name": "tests", "command": "python -B tests/run.py", "source": "tests/run.py"})
        elif (root / "tests").is_dir():
            from .files import read_text_file
            pytest = any((root / marker).is_file() for marker in ("pytest.ini", "conftest.py", "tests/conftest.py"))
            try:
                if "pyproject.toml" in sources:
                    config = tomllib.loads(read_text_file(ctx.workspace.resolve_existing("pyproject.toml").absolute, max_bytes=200_000).text)
                    pytest |= "pytest" in config.get("tool", {})
                for name, directory in ctx.workspace.iter_entries("tests", max_entries=40):
                    if not directory and name.endswith(".py"):
                        text = read_text_file(ctx.workspace.resolve_existing(name).absolute, max_bytes=200_000).text
                        pytest |= "import pytest" in text or "from pytest" in text
            except (ValueError, FileError, OSError):
                pass
            command = "python -m pytest -q" if pytest else "python -m unittest discover -s tests"
            checks.append({"name": "tests", "command": command, "source": "tests/"})
    if "javascript" in types:
        from .files import read_text_file
        try:
            package = json.loads(read_text_file(ctx.workspace.resolve_existing("package.json").absolute, max_bytes=200_000).text)
            for name in ("test", "lint", "typecheck", "build"):
                if isinstance(package.get("scripts"), dict) and name in package["scripts"]:
                    checks.append({"name": name, "command": f"npm run {name}", "source": "package.json"})
        except (ValueError, FileError, AttributeError):
            pass
    if "go" in types:
        checks.extend([{"name": "tests", "command": "go test ./...", "source": "go.mod"},
                       {"name": "build", "command": "go build ./...", "source": "go.mod"},
                       {"name": "vet", "command": "go vet ./...", "source": "go.mod"}])
    if "java" in types:
        if "pom.xml" in sources:
            launcher = ".\\mvnw.cmd" if (root / "mvnw.cmd").is_file() else "./mvnw" if (root / "mvnw").is_file() else "mvn"
            checks.extend({"name": name, "command": f"{launcher} {target}", "source": "pom.xml"}
                          for name, target in (("tests", "test"), ("build", "-DskipTests package")))
        else:
            launcher = ".\\gradlew.bat" if (root / "gradlew.bat").is_file() else "./gradlew" if (root / "gradlew").is_file() else "gradle"
            checks.extend({"name": name, "command": f"{launcher} {target}", "source": "build.gradle"}
                          for name, target in (("tests", "test"), ("build", "assemble")))
    return _result({"workspace": str(root), "project_types": types or ["generic"], "configuration_sources": sources,
                    "entries": [{"path": p, "directory": d} for p, d in entries[:100]], "truncated": len(entries) > 100,
                    "suggested_checks": checks, "instructions": [p for p in ("AGENTS.md", "README.md") if (root / p).is_file()],
                    "capabilities": {"code_outline": ["python", "java_declaration_candidates", "go_declaration_candidates"], "configured_language_servers": sorted(ctx.settings.language_servers),
                                     "semantic_services_enabled": ctx.settings.allow_exec and not ctx.settings.read_only and ctx.settings.execution_mode == "host", "revision": "sha256-bytes", "batch_read": True}})


def code_outline(ctx: ToolContext, args: dict) -> ToolResult:
    resolved = ctx.workspace.resolve_existing(args["path"])
    content, symbols = outline(resolved.absolute)
    start, limit = args.get("offset", 0), args.get("limit", 100)
    selected = symbols[start:start + limit]
    return _result({"path": resolved.relative, "revision": content.revision, "backend": "python_ast" if resolved.absolute.suffix in (".py", ".pyi") else "lexical_candidates", "symbols": selected,
                    "count": len(selected), "total": len(symbols), "next_offset": start + len(selected) if start + len(selected) < len(symbols) else None})


def read_code(ctx: ToolContext, args: dict) -> ToolResult:
    resolved = ctx.workspace.resolve_existing(args["path"])
    content, symbol = select_symbol(resolved.absolute, symbol=args.get("symbol", ""), symbol_id=args.get("symbol_id", ""))
    lines = content.text.split("\n")
    context = args.get("context_lines", 0)
    start = max(1, symbol["start_line"] - context)
    end = min(content.line_count, symbol["end_line"] + context)
    selected = lines[start - 1:end]
    if sum(len(line) + 1 for line in selected) > 60_000:
        raise ToolError("TOO_LARGE", "符号正文超过输出预算，请按行分段读取", {"start_line": start, "end_line": end, "revision": content.revision})
    return _result({"path": resolved.relative, "revision": content.revision, "symbol": symbol,
                    "start_line": start, "end_line": end, "content": "\n".join(selected), "backend": "python_ast" if resolved.absolute.suffix in (".py", ".pyi") else "lexical_candidates"})


def find_symbol(ctx: ToolContext, args: dict) -> ToolResult:
    refresh = ctx.workspace.index.refresh()
    base = ctx.workspace.resolve_existing(args.get("path", ".")).relative
    data = ctx.workspace.index.search(args["query"], base=base, offset=args.get("offset", 0),
                                     limit=args.get("limit", 100), budget_chars=args.get("budget_chars", 30000))
    data["index_refresh"] = refresh
    data["truncated"] |= not refresh["complete"]
    return _result(data)


def batch_read(ctx: ToolContext, args: dict) -> ToolResult:
    from .tools import TOOLS, call_tool
    requests = args["requests"]
    for request in requests:
        name = request["tool"]
        if name == "desktop_observe":
            raise ToolError("DIRECT_CALL_REQUIRED", "窗口观察和图片必须单独调用，不能在 batch_read 中丢弃图像")
        if name == "batch_read" or name not in TOOLS or not TOOLS[name].get("read_only", name in {
            "server_info", "list_dir", "read_file", "read_files", "search_text", "git_status", "git_diff", "git_log"}):
            raise ToolError("READ_ONLY_REQUIRED", f"批量读取只接受非递归的只读工具：{name}")
    def run(request):
        try:
            result = call_tool(request["tool"], ctx, request.get("arguments", {}))
            # 每项限制 30KB，总请求最多 8 项，防止结构化结果绕过输出预算。
            encoded = json.dumps(result.structured, ensure_ascii=False)
            if len(encoded.encode("utf-8")) > 30_000:
                return {"ok": False, "code": "RESULT_TOO_LARGE", "message": "请缩小该项读取范围后重试", "tool": request["tool"]}
            return {"ok": not result.is_error, "tool": request["tool"], "result": result.structured}
        except ToolError as exc:
            return {"ok": False, "tool": request["tool"], "code": exc.code, "message": exc.message, "details": exc.details}
    with ThreadPoolExecutor(max_workers=min(4, len(requests))) as pool:
        results = list(pool.map(run, requests))
    return _result({"results": results, "count": len(results)})


_path = {"type": "string", "maxLength": 2048}
_offset = {"type": "integer", "minimum": 0, "maximum": 50_000}
_limit = {"type": "integer", "minimum": 1, "maximum": 200}
EXTRA_TOOLS = {
    "project_overview": spec("首次进入项目时使用：查看项目类型、入口配置、目录重点与建议检查命令；不执行任何命令。", project_overview, {}),
    "find_files": spec("按 glob 查找工作区文件，支持 ** 与分页；返回相对路径。", find_files,
                       {"path": _path, "pattern": {"type": "string", "maxLength": 500}, "offset": _offset, "limit": _limit}),
    "code_outline": spec("读取 Python 类、函数、方法的大纲和签名，返回可精确定位的 symbol_id 与文件版本。", code_outline,
                         {"path": _path, "offset": _offset, "limit": _limit}, ["path"]),
    "read_code": spec("按 Python qualified_name 或大纲返回的 symbol_id 读取完整定义，含装饰器；过期符号会被拒绝。", read_code,
                      {"path": _path, "symbol": {"type": "string"}, "symbol_id": {"type": "string"},
                       "context_lines": {"type": "integer", "minimum": 0, "maximum": 30}}, ["path"]),
    "find_symbol": spec("按名称查找工作区 Python 定义，返回作用域、签名、位置和版本；不把文本命中当作语义引用。", find_symbol,
                        {"query": {"type": "string", "minLength": 1, "maxLength": 200}, "path": _path, "limit": _limit}, ["query"]),
    "batch_read": spec("一次并行执行最多 8 个独立只读请求；保留请求顺序、逐项错误和输出预算。", batch_read,
                       {"requests": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "object", "properties": {
                           "tool": {"type": "string"}, "arguments": {"type": "object"}}, "required": ["tool"], "additionalProperties": False}}}, ["requests"]),
}
