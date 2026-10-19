# -*- mode: python ; coding: utf-8 -*-

# 双击即进图形界面；内置 cloudflared，同事拿到就能用，不需要装 Python 或任何东西。

a = Analysis(
    ['gui_launcher.py'],
    pathex=['.'],
    binaries=[],
    datas=[('bin/cloudflared.exe', 'bin'), ('chatcode/desktop_worker.py', 'desktop_backend'), ('bin/python-sandbox', 'bin/python-sandbox')],
    hiddenimports=['chatcode.gui', 'chatcode.cli', 'chatcode.tunnel', 'chatcode.desktop_worker'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'PIL', 'numpy', 'windows_mcp', 'psutil', 'comtypes'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ChatCodex',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
