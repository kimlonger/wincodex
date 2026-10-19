"""Python AST 大纲与符号定位；缓存受文件内容和数量限制。"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path

from .files import FileError, read_text_file


@lru_cache(maxsize=24)
def _python_outline(source: str, revision: str) -> tuple[dict, ...]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise FileError("SYNTAX_ERROR", f"Python 语法错误：{exc.msg}", {"line": exc.lineno, "column": exc.offset}) from None
    symbols = []
    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scope = []
        def add(self, node):
            qualified = ".".join([*self.scope, node.name])
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            end = node.end_lineno or node.lineno
            if isinstance(node, ast.ClassDef):
                kind = "class"
                signature = f"class {node.name}(" + ", ".join(ast.unparse(b) for b in node.bases) + ")"
            else:
                kind = "function"
                signature = ("async " if isinstance(node, ast.AsyncFunctionDef) else "") + f"def {node.name}({ast.unparse(node.args)})"
                if node.returns:
                    signature += " -> " + ast.unparse(node.returns)
            definition = source.split("\n")[node.lineno - 1]
            name_match = re.search(r"\b(?:def|class)\s+(" + re.escape(node.name) + r")\b", definition)
            symbols.append({"name": node.name, "qualified_name": qualified, "kind": kind,
                            "start_line": start, "definition_line": node.lineno, "end_line": end,
                            "signature": signature[:1000], "symbol_id": f"{revision}:{start}:{end}"})
            symbols[-1]["column"] = name_match.start(1) + 1 if name_match else None
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()
        visit_ClassDef = add
        visit_FunctionDef = add
        visit_AsyncFunctionDef = add
    Visitor().visit(tree)
    return tuple(symbols)


def outline(path: Path):
    content = read_text_file(path, max_bytes=2 * 1024 * 1024)
    if path.suffix.lower() in (".py", ".pyi"):
        return content, [dict(item) for item in _python_outline(content.text, content.revision)]
    return content, lexical_outline(content.text, content.revision, path.suffix.lower())


def lexical_outline(source, revision, suffix):
    """Java/Go 的导航候选，不声称类型解析或可用于语义重命名。"""
    if suffix not in (".java", ".go"):
        raise FileError("CAPABILITY_UNAVAILABLE", "内置大纲支持 Python；Java/Go 提供声明候选；其他语言需 LSP")
    # 去除注释和字面量，保留位置；防止注释中的伪声明进入大纲。
    masked = re.sub(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\x27(?:\\.|[^\x27\\])*\x27|`[^`]*`',
                    lambda m: "".join("\n" if c == "\n" else " " for c in m.group()), source)
    stack, pairs = [], {}
    for at, char in enumerate(masked):
        if char == "{":
            stack.append(at)
        elif char == "}" and stack:
            pairs[stack.pop()] = at
    pattern = (r'(?m)^\s*(?:func\s+(?:\([^\n)]*\)\s*)?(?P<function>\w+)|type\s+(?P<type>\w+)\s+(?:struct|interface))\b'
               if suffix == ".go" else
               r'(?m)^[ \t]*(?:(?:public|private|protected|static|final|abstract|synchronized|native|default|strictfp|sealed)\s+)*(?:(?:class|interface|enum|record)\s+(?P<type>\w+)|(?:[\w<>\[\],.?]+[ \t]+)+(?P<function>\w+)\s*\()')
    symbols = []
    for match in re.finditer(pattern, masked):
        name = match.group("function") or match.group("type")
        if name in {"if", "for", "while", "switch", "catch", "return", "new"}:
            continue
        name_at = match.start("function") if match.group("function") else match.start("type")
        line = source.count("\n", 0, name_at) + 1
        tail = masked[match.end():match.end() + 1000]
        ender = re.search(r"[;{]", tail)
        begin = match.end() + ender.start() if ender else name_at
        end = pairs.get(begin, begin)
        end_line = source.count("\n", 0, end) + 1
        symbols.append({"name": name, "qualified_name": name, "kind": "type" if match.group("type") else "function",
                        "start_line": line, "definition_line": line, "end_line": end_line,
                        "column": name_at - source.rfind("\n", 0, name_at), "signature": source[name_at:begin].strip()[:500],
                        "symbol_id": f"{revision}:{line}:{end_line}", "semantic": False, "backend": "lexical_candidates"})
        if len(symbols) >= 2001:
            break
    return symbols


def select_symbol(path: Path, *, symbol: str = "", symbol_id: str = ""):
    content, symbols = outline(path)
    if symbol_id:
        if not symbol_id.startswith(content.revision + ":"):
            raise FileError("REVISION_MISMATCH", "符号对应的文件已经变化，请重新获取大纲")
        matches = [item for item in symbols if item["symbol_id"] == symbol_id]
    else:
        matches = [item for item in symbols if item["qualified_name"] == symbol]
        if not matches:
            matches = [item for item in symbols if item["name"] == symbol]
    if len(matches) != 1:
        raise FileError("AMBIGUOUS_SYMBOL" if matches else "SYMBOL_NOT_FOUND", "请使用唯一的 qualified_name 或 symbol_id 定位符号",
                        {"candidates": matches[:20]})
    return content, matches[0]
