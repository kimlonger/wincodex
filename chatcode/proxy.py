"""读取用户配置的代理；固定端口扫描仅作为最后的兼容回退。"""

from dataclasses import dataclass
import os
import socket
from urllib.parse import urlsplit, urlunsplit


COMMON_PROXY_PORTS = (7897, 7890, 7892, 7891, 7893, 10809, 1080, 10808, 20171, 2080, 8889)


@dataclass(frozen=True)
class ProxyDetection:
    proxy: str | None
    source: str
    message: str


def display_proxy(value):
    """日志隐藏代理 URL 中的用户名和密码。"""
    parsed = urlsplit(value)
    if parsed.username is None:
        return value
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port is not None:
        host += f":{parsed.port}"
    return urlunsplit((parsed.scheme, "***@" + host, parsed.path, parsed.query, parsed.fragment))


def _normalize_proxy(value, scheme="http"):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if any(char.isspace() for char in value):
        return None
    if "://" not in value:
        value = f"{scheme}://{value}"
    if value.startswith("socks://"):
        value = "socks5://" + value[len("socks://"):]
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https", "socks4", "socks5", "socks5h") or not parsed.hostname:
            return None
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return None
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return None
    except ValueError:
        return None
    return value.rstrip("/")


def _windows_proxy_settings():
    if os.name != "nt":
        return {}, False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            def read(name, default=None):
                try:
                    return winreg.QueryValueEx(key, name)[0]
                except OSError:
                    return default
            enabled, raw, pac = read("ProxyEnable", 0), read("ProxyServer", ""), bool(read("AutoConfigURL", ""))
    except OSError:
        return {}, False
    if not enabled or not isinstance(raw, str):
        return {}, pac
    if "=" not in raw:
        return {"https": raw, "http": raw}, pac
    proxies = {}
    for entry in raw.split(";"):
        if "=" in entry:
            protocol, value = entry.split("=", 1)
            proxies[protocol.strip().lower()] = value.strip()
    return proxies, pac


def port_is_open(host, port, timeout=0.35):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def detect_proxy():
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        proxy = _normalize_proxy(os.environ.get(name))
        if proxy:
            return ProxyDetection(proxy, f"环境变量 {name}", "已读取显式代理配置")
    proxies, pac = _windows_proxy_settings()
    for protocol in ("https", "http", "socks"):
        proxy = _normalize_proxy(proxies.get(protocol), "socks5" if protocol == "socks" else "http")
        if proxy:
            return ProxyDetection(proxy, "Windows 系统代理", "已读取当前用户的系统代理设置")
    for port in COMMON_PROXY_PORTS:
        if port_is_open("127.0.0.1", port):
            return ProxyDetection(f"http://127.0.0.1:{port}", "常用端口扫描", "检测到开放端口，尚未验证代理协议")
    if pac:
        return ProxyDetection(None, "Windows PAC", "检测到自动配置脚本（PAC），请填写代理软件的 HTTP/mixed 地址；当前程序不执行 PAC 脚本")
    return ProxyDetection(None, "未发现", "未发现环境变量、Windows 手动代理或常用本地端口；若使用 TUN/VPN，可由系统接管流量")
