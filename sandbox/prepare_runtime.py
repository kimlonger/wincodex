"""发行构建准备：验证并展开官方 Python 嵌入式运行时；终端用户 exe 已内置。"""

import hashlib
from pathlib import Path
import tempfile
import urllib.request
import zipfile

URL = "https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip"
SHA256 = "d297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15"


def main():
    target = Path(__file__).resolve().parent.parent / "bin" / "python-sandbox"
    if target.exists():
        raise SystemExit(f"目录已存在，不覆盖：{target}")
    target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chatcode-runtime-build-") as directory:
        archive = Path(directory) / "python.zip"
        used, digest = 0, hashlib.sha256()
        with urllib.request.urlopen(URL, timeout=60) as src, archive.open("wb") as dst:
            while block := src.read(1024 * 1024):
                used += len(block)
                if used > 16 * 1024 * 1024:
                    raise SystemExit("运行时下载超过 16 MiB")
                dst.write(block)
                digest.update(block)
        if digest.hexdigest() != SHA256:
            raise SystemExit("运行时 SHA256 不匹配，未安装")
        with zipfile.ZipFile(archive) as bundle:
            if any(not (target / name).resolve().is_relative_to(target) for name in bundle.namelist()):
                raise SystemExit("运行时 ZIP 路径无效")
            bundle.extractall(target)
    print(target)


if __name__ == "__main__":
    main()
