"""本机工具链检测；只发现，不下载、不远程启用进程。"""

import os
from pathlib import Path
import shutil

from .config import app_data_dir
from .files import bytes_revision


def detect(workspace):
    base = app_data_dir() / "language-runtime"
    gopls = shutil.which("gopls") or next((str(p) for p in (base / "bin" / "gopls.exe", base / "bin" / "gopls") if p.is_file()), None)
    java = shutil.which("java")
    jars = sorted((base / "jdtls" / "plugins").glob("org.eclipse.equinox.launcher_*.jar"))
    platform = "config_win" if os.name == "nt" else "config_mac" if __import__("sys").platform == "darwin" else "config_linux"
    commands = {}
    if gopls:
        commands["go"] = [gopls]
    if java and len(jars) == 1 and (base / "jdtls" / platform).is_dir():
        cache = app_data_dir() / "language-cache" / bytes_revision(os.path.normcase(str(workspace)).encode())[:24]
        commands["java"] = [java, "-Djava.import.generatesMetadataFilesAtProjectRoot=false", "-Declipse.application=org.eclipse.jdt.ls.core.id1", "-Dosgi.bundles.defaultStartLevel=4",
            "-Declipse.product=org.eclipse.jdt.ls.core.product", "-Dlog.level=WARNING", "-Xmx768m", "--add-modules=ALL-SYSTEM",
            "--add-opens", "java.base/java.util=ALL-UNNAMED", "--add-opens", "java.base/java.lang=ALL-UNNAMED",
            "-jar", str(jars[0]), "-configuration", str(base / "jdtls" / platform), "-data", "{workspace_cache}"]
    return {"executables": {name: shutil.which(name) for name in ("go", "java", "javac", "mvn", "gradle")},
            "detected_language_servers": commands, "automatically_enabled": False,
            "notice": "检测不启动程序；Java 需支持当前 JDT LS 的 JDK；请在本机高级设置确认命令。"}


def status(ctx, args):
    import json
    from .tool_types import ToolResult
    result = detect(ctx.workspace.root)
    result["configured"] = sorted(ctx.settings.language_servers)
    manager = ctx.workspace._language_services
    result["sessions"] = ({name: {"ready": s.ready.is_set(), "error": s.error, "alive": s.process.poll() is None,
                                  "capabilities": {key: s.capabilities.get(key, False) for key in ("documentSymbolProvider", "referencesProvider", "renameProvider")}}
                           for name, s in manager.sessions.items()} if manager else {})
    result["enabled"] = ctx.settings.allow_exec and not ctx.settings.read_only and ctx.settings.execution_mode == "host"
    return ToolResult(json.dumps(result, ensure_ascii=False), result)
