"""保存最近项目的非秘密 GUI 配置；命令行显式参数始终优先。"""

import argparse
import json
from pathlib import Path

from .config import app_data_dir, build_arg_parser
from .files import atomic_write_bytes


_FIELDS = {"port": int, "auth_mode": str, "tunnel": str, "proxy": str, "host": str,
           "public_url": str, "tunnel_binary": str, "frp_server_addr": str, "frp_server_port": int,
           "no_proxy": bool, "no_exec": bool, "no_gzip": bool, "read_only": bool,
           "execution_mode": str, "sandbox_memory_mb": int, "storage_quota_mb": int}


def _load():
    try:
        path = app_data_dir() / "gui_profiles.json"
        if path.stat().st_size > 128_000:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) and isinstance(value.get("profiles", {}), dict) else {}
    except (OSError, ValueError):
        return {}


def load_profile(args):
    result = argparse.Namespace(**vars(args))
    data = _load()
    workspace = args.workspace or data.get("last_workspace") or str(Path.cwd())
    if not isinstance(workspace, str):
        workspace = str(Path.cwd())
    try:
        canonical = str(Path(workspace).resolve())
    except (OSError, ValueError, UnicodeError):
        workspace = canonical = str(Path.cwd())
    result.workspace = workspace
    profiles = data.get("profiles", {})
    profile = profiles.get(canonical)
    if profile is None:
        # Preserve profiles previously stored using a relative path or a Windows 8.3 alias.
        for stored_path, stored_profile in reversed(list(profiles.items())):
            try:
                if Path(stored_path).resolve() == Path(canonical):
                    profile = stored_profile
                    break
            except (OSError, ValueError, UnicodeError):
                continue
    if profile is None:
        profile = {}
    explicit = getattr(args, "_provided_options", None)
    defaults = build_arg_parser().parse_args([])
    for name, kind in _FIELDS.items():
        if not isinstance(profile, dict):
            break
        value = profile.get(name)
        provided = name in explicit if explicit is not None else getattr(args, name) != getattr(defaults, name)
        if provided or type(value) is not kind:
            continue
        if name in ("port", "frp_server_port") and not 1 <= value <= 65535:
            continue
        if name == "auth_mode" and value not in ("path", "oauth", "bearer", "none"):
            continue
        if name == "tunnel" and value not in ("cloudflared", "cpolar", "frp", "none"):
            continue
        if name == "execution_mode" and value not in ("host", "native"):
            continue
        if name == "sandbox_memory_mb" and not 128 <= value <= 8192:
            continue
        if name == "storage_quota_mb" and not 128 <= value <= 65536:
            continue
        setattr(result, name, value)
    configured = profile.get("language_servers") if isinstance(profile, dict) else None
    if "language_server" not in (explicit or set()) and isinstance(configured, dict):
        valid = {language: argv for language, argv in configured.items()
                 if isinstance(language, str) and isinstance(argv, list) and argv
                 and all(isinstance(part, str) and part and "\0" not in part for part in argv)}
        result.language_server = list(valid.items())
    return result, list(data.get("profiles", {}))[-20:]


def save_profile(settings):
    data = _load()
    profile = {key: getattr(settings, key) for key in _FIELDS if not key.startswith("no_")}
    profile.update(no_exec=not settings.allow_exec, no_proxy=not settings.auto_proxy, no_gzip=not settings.gzip)
    profile["language_servers"] = settings.language_servers
    if not profile["proxy"] or "@" in profile["proxy"]:
        profile["proxy"] = ""
    profiles = data.get("profiles", {})
    workspace = str(Path(settings.workspace).resolve())
    profiles.pop(workspace, None)
    profiles[workspace] = profile
    data = {"last_workspace": workspace, "profiles": dict(list(profiles.items())[-20:])}
    atomic_write_bytes(app_data_dir() / "gui_profiles.json", json.dumps(data, ensure_ascii=False).encode(), mode=0o600)
