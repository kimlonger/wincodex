"""安装可选的独立桌面运行环境；不自动绑定窗口或启用输入。"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable, help="已安装的 Python 3.14 路径")
    parser.add_argument("--target", type=Path, default=None)
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("桌面后端目前只支持 Windows")
    version = json.loads(subprocess.check_output([args.python, "-I", "-c", "import sys,json;print(json.dumps(list(sys.version_info[:2])))"]))
    if version != [3, 14]:
        raise SystemExit("本锁定环境要求 Python 3.14，请用 --python 指定其 python.exe")
    target = (args.target or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "chatcode-mcp" / "desktop-runtime-v0.10").resolve()
    if target.exists() and not (target / "pyvenv.cfg").is_file():
        raise SystemExit("目标目录已存在且不是虚拟环境，未覆盖")
    subprocess.run([args.python, "-m", "venv", str(target)], check=True)
    python = target / "Scripts" / "python.exe"
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(Path(__file__).with_name("requirements.lock"))], check=True)
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
    print("桌面后端已安装；GUI 桌面设置中填入：", python)


if __name__ == "__main__":
    main()
