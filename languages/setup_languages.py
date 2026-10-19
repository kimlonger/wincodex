"""本机可选安装器：Java/Go 语言服务；不修改项目、系统 PATH 或连接权限。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatcode.config import app_data_dir
from chatcode.language_setup import detect

JDT_URL = "https://download.eclipse.org/jdtls/milestones/1.55.0/jdt-language-server-1.55.0-202601131729.tar.gz"
JDT_SHA256 = "90627c9f03704dbb404f37651625d0751c078ab7534246023d53adcea1411b91"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", choices=("go", "java", "both"), default="both")
    parser.add_argument("--go-proxy", default="https://proxy.golang.org", help="可选 Go 模块代理；仍使用 Go checksum 验证")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    base = app_data_dir() / "language-runtime"
    base.mkdir(parents=True, exist_ok=True)
    if args.language in ("go", "both"):
        if not shutil.which("go"):
            raise SystemExit("先安装 Go 工具链")
        environment = os.environ.copy()
        environment.update(GOBIN=str(base / "bin"), GOPROXY=args.go_proxy)
        subprocess.run([shutil.which("go"), "install", "golang.org/x/tools/gopls@v0.21.1"], env=environment, check=True)
    if args.language in ("java", "both"):
        if not shutil.which("java"):
            raise SystemExit("先安装 JDK 21 或兼容的新版 JDK")
        target = base / "jdtls"
        if target.exists():
            raise SystemExit(f"目标已存在，不覆盖：{target}")
        with tempfile.TemporaryDirectory(dir=base, prefix="jdt-download-") as directory:
            archive = Path(directory) / "jdt.tar.gz"
            digest, used = hashlib.sha256(), 0
            with urllib.request.urlopen(JDT_URL, timeout=60) as src, archive.open("wb") as dst:
                while block := src.read(1024 * 1024):
                    used += len(block)
                    if used > 256 * 1024 * 1024:
                        raise SystemExit("下载超过 256 MiB，已停止")
                    digest.update(block)
                    dst.write(block)
            if digest.hexdigest() != JDT_SHA256:
                raise SystemExit("JDT LS SHA256 不匹配，未安装")
            unpacked = Path(directory) / "unpacked"
            unpacked.mkdir()
            with tarfile.open(archive) as bundle:
                bundle.extractall(unpacked, filter="data")
            unpacked.rename(target)
    print(json.dumps(detect(args.workspace.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
