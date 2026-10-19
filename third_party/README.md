# Third-party components

The core server uses the Python standard library. Generated/downloaded binaries are not checked into Git. Windows release builds include the following components; retain their notices when redistributing the bundle.

| Component | Source and version | License |
| --- | --- | --- |
| cloudflared | [Cloudflare official release 2026.10.1](https://github.com/cloudflare/cloudflared/releases/tag/2026.10.1) | Apache-2.0; see [cloudflared-LICENSE.txt](cloudflared-LICENSE.txt) and upstream notices |
| CPython embeddable x64 runtime | [Python 3.14.7](https://www.python.org/downloads/release/python-3147/) | Python Software Foundation and included component licenses; see [CPython-LICENSE.txt](CPython-LICENSE.txt) |
| PyInstaller bootloader/build tooling | [PyInstaller](https://pyinstaller.org/) (build pin 6.21.0) | GPL with the project's distribution exception; see [PyInstaller-LICENSE.txt](PyInstaller-LICENSE.txt) |

`scripts/prepare_build.py` verifies the official cloudflared binary SHA256. `sandbox/prepare_runtime.py` verifies the official CPython archive SHA256. The release's checksum file covers the distributed executable and accompanying files.

Optional Java/Go language servers and Windows-MCP desktop packages are installed separately. Their pinned sources and licenses are recorded in [NOTICE](../NOTICE), `languages/setup_languages.py` and `desktop/requirements.lock`. Their own distributions retain their license metadata. The desktop backend is not bundled into the launcher and is not enabled by the normal coding workflow.

Core design references and attribution are retained in [NOTICE](../NOTICE). Apache-2.0 licensing of this repository does not replace licenses on third-party components.
