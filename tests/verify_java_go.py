"""真实 Java/Go 服务：大纲、跨文件引用/重命名、同名隔离、字节撤回、编译与测试。"""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatcode.config import Settings
from chatcode.files import read_text_file
from chatcode.language_setup import detect
from chatcode.lsp import services
from chatcode.tools import ToolContext, ToolError, call_tool
from chatcode.workspace import Workspace


def verify(language, base, commands):
    root = base / language
    root.mkdir()
    if language == "go":
        files = {"go.mod": "module example.com/chatcodefixture\n\ngo 1.24\n",
            "a.go": 'package sample\n\nfunc Greet() string { return "hello" }\n',
            "b.go": "package sample\n\nfunc Use() string { return Greet() }\n",
            "unrelated.go": 'package sample\n\ntype Other struct{}\nfunc (Other) Greet() string { return "other" }\n',
            "a_test.go": 'package sample\nimport "testing"\nfunc TestGreeting(t *testing.T) { if Use() != "hello" { t.Fatal("wrong result") } }\n'}
        source, line, column, name, changed_name = "a.go", 3, 6, "Greet", "Welcome"
        check = ["go", "test", "./..."]
    else:
        files = {"pom.xml": '<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>chatcode-fixture</artifactId><version>1.0</version><properties><maven.compiler.source>17</maven.compiler.source><maven.compiler.target>17</maven.compiler.target></properties></project>',
            "src/main/java/sample/Greeter.java": 'package sample;\npublic class Greeter {\n    public static String greet() { return "hello"; }\n}\n',
            "src/main/java/sample/Use.java": 'package sample;\npublic class Use {\n    public static void main(String[] args) {\n        if (!Greeter.greet().equals("hello")) throw new AssertionError();\n        System.out.println("JAVA_TEST_OK");\n    }\n}\n',
            "src/main/java/sample/Other.java": 'package sample;\npublic class Other {\n    public String greet() { return "other"; }\n}\n'}
        source, line, column, name, changed_name = "src/main/java/sample/Greeter.java", 3, 26, "greet", "welcome"
        check = ["javac", "-d", "classes", "src/main/java/sample/Greeter.java", "src/main/java/sample/Use.java", "src/main/java/sample/Other.java"]
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())
    before = {p: (root / p).read_bytes() for p in files}
    ctx = ToolContext(Workspace(root), Settings(workspace=root, language_servers={language: commands[language]}, tunnel="none"))
    def call(tool, **args):
        deadline = time.monotonic() + 120
        while True:
            try:
                return call_tool(tool, ctx, args).structured
            except ToolError as exc:
                if exc.code not in ("LSP_STARTING", "LSP_TIMEOUT") or time.monotonic() > deadline:
                    raise
                time.sleep(.5)
    try:
        outline = call("code_outline", path=source, backend="lsp")
        assert any(s["name"] == name or s["name"].startswith(name + "(") for s in outline["symbols"]), outline
        # 语义索引可能在 initialize 完成后继续导入项目；只读观察可重试。
        deadline = time.monotonic() + 100
        while True:
            refs = call("find_references", path=source, line=line, column=column)
            if refs["semantic"] and len({r["path"] for r in refs["references"]}) >= 2:
                break
            if time.monotonic() > deadline:
                raise AssertionError(refs)
            time.sleep(1)
        assert not any("Other" in r["path"] or "unrelated" in r["path"] for r in refs["references"]), refs
        args = dict(path=source, line=line, column=column, new_name=changed_name, expected_revision=read_text_file(root / source).revision)
        call("rename_symbol", **args, dry_run=True)
        assert all((root / p).read_bytes() == data for p, data in before.items() if Path(p).suffix in (".java", ".go"))
        # 修改请求只提交一次，不把语言服务导入失败当作重试授权。
        changed = call_tool("rename_symbol", ctx, {**args, "operation_id": "real-rename"}).structured
        assert changed["semantic"] and changed["count"] >= 2, changed
        untouched = "unrelated.go" if language == "go" else "src/main/java/sample/Other.java"
        assert (root / untouched).read_bytes() == before[untouched]
        process = call("run_checks", name="tests", argv=check, yield_time_ms=10000, timeout=120)
        while process["running"]:
            process = call("read_process", process_id=process["process_id"], wait_ms=10000)
        assert process["exit_code"] == 0, process
        if language == "java":
            process = call("run_checks", name="runtime-test", argv=["java", "-cp", "classes", "sample.Use"], yield_time_ms=10000)
            assert process["exit_code"] == 0 and "JAVA_TEST_OK" in process["stdout"], process
        call("undo_changes", change_id=changed["change_id"])
        assert all((root / p).read_bytes() == data for p, data in before.items() if Path(p).suffix in (".java", ".go"))
        assert not any((root / p).exists() for p in (".project", ".classpath", ".settings"))
        return {"language": language, "lsp_outline": True, "semantic_reference_files": sorted({r["path"] for r in refs["references"]}),
                "cross_file_rename": True, "unrelated_same_name_preserved": True, "dry_run": True,
                "byte_exact_source_undo": True, "compiled_and_tested": True, "no_eclipse_metadata_in_project": True,
                "language_service_generated_metadata_changes": [p for p, data in before.items() if Path(p).suffix not in (".java", ".go") and (root / p).read_bytes() != data]}
    finally:
        services(ctx).close()
        if ctx.workspace._processes:
            ctx.workspace.processes.shutdown()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    commands = detect(Path.cwd())["detected_language_servers"]
    assert {"go", "java"} <= commands.keys(), "先运行 languages/setup_languages.py"
    with tempfile.TemporaryDirectory(prefix="chatcode-java-go-") as directory:
        with patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(Path(directory) / "state"), "GOTOOLCHAIN": "local"}):
            result = {"checks": [verify(language, Path(directory), commands) for language in ("go", "java")]}
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
