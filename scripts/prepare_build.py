"""Prepare ignored Windows build inputs from pinned official sources."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
CLOUDFLARED_URL = "https://github.com/cloudflare/cloudflared/releases/download/2026.10.1/cloudflared-windows-amd64.exe"
CLOUDFLARED_SHA256 = "4d2490e74082c5d48dbfedaf92576fa5bd1702379ab88eb7faa0cfbe8dc891b6"


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def main():
    if os.name != "nt":
        raise SystemExit("The packaged launcher is built on Windows.")
    target = ROOT / "bin/cloudflared.exe"
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        if digest(target) != CLOUDFLARED_SHA256:
            raise SystemExit("Existing cloudflared differs from the pinned build input; not overwritten.")
    else:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                temporary = Path(output.name)
                used = 0
                with urllib.request.urlopen(CLOUDFLARED_URL, timeout=60) as response:
                    while block := response.read(1024 * 1024):
                        used += len(block)
                        if used > 128 * 1024 * 1024:
                            raise SystemExit("Tunnel download exceeds 128 MiB.")
                        output.write(block)
            if digest(temporary) != CLOUDFLARED_SHA256:
                raise SystemExit("Tunnel SHA256 mismatch; build input not installed.")
            temporary.replace(target)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
    print("Verified cloudflared 2026.10.1")
    runtime = ROOT / "bin/python-sandbox"
    if not runtime.exists():
        subprocess.run([sys.executable, "-B", str(ROOT / "sandbox/prepare_runtime.py")], check=True)
    elif not all((runtime / name).is_file() for name in ("python.exe", "python314.zip", "LICENSE.txt")):
        raise SystemExit("Existing embedded runtime is incomplete; not overwritten.")
    print("Embedded Python build input is ready. Fresh installs are verified by sandbox/prepare_runtime.py.")


if __name__ == "__main__":
    main()
