"""OAuth 2.1 + PKCE，供 ChatGPT / claude.ai 这类云端客户端接入。

为什么必须自己实现 OAuth：ChatGPT 的自定义连接器表单只给 OAuth 和"无认证"两个选项，
没有"填一个静态 Bearer 头"的地方。所以要让网页版能连进来，服务器侧必须把授权码
流程跑通。

设计取舍：
  - 使用 authorization_code + PKCE(S256)，支持绑定客户端与资源的刷新令牌轮换。
    access token 默认 24 小时，refresh token 默认 30 天；令牌存放在内存，重启后需重新授权。
  - 支持 RFC 7591 动态客户端注册：ChatGPT 会自己注册，不用手填 client id/secret。
  - issuer/resource 从请求动态推导，因为 trycloudflare 域名每次启动都变。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import secrets
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import MAX_PENDING_CODES, OAUTH_CODE_TTL_SECONDS
from .files import atomic_write_bytes


class OAuthError(Exception):
    def __init__(self, code: str, description: str, status: int = 400) -> None:
        super().__init__(description)
        self.code = code
        self.description = description
        self.status = status


@dataclass
class OAuthClient:
    client_id: str
    redirect_uris: list[str]
    client_secret: str | None
    token_endpoint_auth_method: str
    client_name: str = ""


@dataclass
class AuthCode:
    code: str
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    resource: str
    expires_at: float
    scope: str = ""


@dataclass
class AccessToken:
    token: str
    client_id: str
    resource: str
    expires_at: float
    scope: str = ""


@dataclass
class RefreshToken:
    client_id: str
    resource: str
    expires_at: float
    scope: str = ""


@dataclass
class OAuthServer:
    password: str
    token_ttl: int = 86_400
    _clients: dict[str, OAuthClient] = field(default_factory=dict)
    _codes: dict[str, AuthCode] = field(default_factory=dict)
    _tokens: dict[str, AccessToken] = field(default_factory=dict)
    # refresh_token -> (client_id, expires_at)
    _refresh: dict[str, RefreshToken] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _store_path: Path | None = None

    def __post_init__(self) -> None:
        # 客户端注册信息落盘：否则每次重启服务，ChatGPT 注册的 client 就丢了，
        # 用户又得重新授权一遍。
        if self._store_path is None:
            from .config import app_data_dir

            self._store_path = app_data_dir() / "oauth_clients.json"
        self._load_clients()

    def _load_clients(self) -> None:
        try:
            raw = json.loads(self._store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(raw, dict):
            return
        for client_id, item in list(raw.items())[:512]:
            if not isinstance(item, dict):
                continue
            uris = item.get("redirect_uris")
            method = item.get("token_endpoint_auth_method", "none")
            secret = item.get("client_secret")
            if not isinstance(uris, list) or not uris or not all(isinstance(uri, str) for uri in uris):
                continue
            if method not in ("none", "client_secret_post", "client_secret_basic"):
                continue
            if secret is not None and not isinstance(secret, str):
                continue
            self._clients[client_id] = OAuthClient(
                client_id=client_id,
                redirect_uris=list(uris),
                client_secret=item.get("client_secret"),
                token_endpoint_auth_method=item.get("token_endpoint_auth_method", "none"),
                client_name=item.get("client_name", ""),
            )

    def _save_clients(self) -> None:
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                cid: {
                    "redirect_uris": c.redirect_uris,
                    "client_secret": c.client_secret,
                    "token_endpoint_auth_method": c.token_endpoint_auth_method,
                    "client_name": c.client_name,
                }
                for cid, c in self._clients.items()
            }
            atomic_write_bytes(self._store_path, json.dumps(payload, ensure_ascii=False).encode("utf-8"), mode=0o600)
        except OSError:
            # 落盘失败不影响本次会话，只是重启后要重新授权
            pass

    # ---------- 动态注册 ----------

    def register_client(self, payload: dict[str, Any]) -> dict[str, Any]:
        redirect_uris = payload.get("redirect_uris")
        if not isinstance(redirect_uris, list) or not 1 <= len(redirect_uris) <= 10:
            raise OAuthError("invalid_client_metadata", "redirect_uris 必填且必须是数组")
        for uri in redirect_uris:
            if not isinstance(uri, str):
                raise OAuthError("invalid_client_metadata", "redirect_uris 元素必须是字符串")
            try:
                parsed = urllib.parse.urlparse(uri)
                valid = (len(uri) <= 2048 and parsed.hostname and not parsed.fragment
                         and parsed.username is None and parsed.password is None
                         and (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"))))
                parsed.port
            except ValueError:
                valid = False
            if not valid:
                raise OAuthError("invalid_redirect_uri", "回调地址必须是 HTTPS 或回环 HTTP 地址，且不能含凭据和 fragment")

        method = payload.get("token_endpoint_auth_method", "none")
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            raise OAuthError("invalid_client_metadata", f"不支持的认证方式：{method}")

        client_id = secrets.token_urlsafe(16)
        secret = secrets.token_urlsafe(24) if method != "none" else None
        client = OAuthClient(
            client_id=client_id,
            redirect_uris=list(redirect_uris),
            client_secret=secret,
            token_endpoint_auth_method=method,
            client_name=str(payload.get("client_name", ""))[:120],
        )
        with self._lock:
            if len(self._clients) >= 512:
                raise OAuthError("temporarily_unavailable", "注册客户端数量已达上限", status=503)
            self._clients[client_id] = client
            self._save_clients()
        body: dict[str, Any] = {
            "client_id": client_id,
            "redirect_uris": client.redirect_uris,
            "token_endpoint_auth_method": method,
            "client_name": client.client_name,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }
        if secret:
            body["client_secret"] = secret
            body["client_secret_expires_at"] = 0
        return body

    def get_client(self, client_id: str) -> OAuthClient | None:
        with self._lock:
            return self._clients.get(client_id)

    def authenticate_client(self, client_id: str, client_secret: str | None = None, method: str | None = None) -> None:
        client = self.get_client(client_id)
        if client is None:
            raise OAuthError("invalid_client", "未知的客户端", status=401)
        if method is not None and method != client.token_endpoint_auth_method:
            raise OAuthError("invalid_client", "客户端认证方式与注册配置不一致", status=401)
        if client.token_endpoint_auth_method != "none":
            if not client_secret or not hmac.compare_digest(client_secret.encode("utf-8"), (client.client_secret or "").encode("utf-8")):
                raise OAuthError("invalid_client", "客户端凭据无效", status=401)

    # ---------- 授权 ----------

    def create_authorization_code(
        self,
        *,
        client_id: str,
        redirect_uri: str,
        code_challenge: str,
        code_challenge_method: str,
        resource: str,
        scope: str = "",
    ) -> str:
        client = self.get_client(client_id)
        if client is None:
            raise OAuthError("invalid_client", "未知的 client_id")
        if redirect_uri not in client.redirect_uris:
            raise OAuthError("invalid_redirect_uri", "回调地址与注册时不一致")
        if code_challenge_method != "S256" or re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge) is None:
            raise OAuthError("invalid_request", "必须提供有效的 S256 PKCE challenge")

        with self._lock:
            # 顺手清掉过期授权码，避免有人反复触发注册把内存撑大
            now = time.time()
            for key in [k for k, v in self._codes.items() if v.expires_at < now]:
                self._codes.pop(key, None)
            if len(self._codes) >= MAX_PENDING_CODES:
                raise OAuthError("temporarily_unavailable", "待处理授权码过多，请稍后再试", status=503)
            code = secrets.token_urlsafe(24)
            self._codes[code] = AuthCode(
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                resource=resource,
                expires_at=now + OAUTH_CODE_TTL_SECONDS,
                scope=scope,
            )
        return code

    def exchange_code(
        self, *, code: str, client_id: str, redirect_uri: str,
        code_verifier: str, resource: str, client_secret: str | None = None,
    ) -> dict[str, Any]:
        self.authenticate_client(client_id, client_secret)
        with self._lock:
            record = self._codes.get(code)
        if record is None or record.expires_at < time.time():
            raise OAuthError("invalid_grant", "授权码无效或已过期")
        if record.client_id != client_id or record.redirect_uri != redirect_uri:
            raise OAuthError("invalid_grant", "授权码与客户端或回调地址不匹配")
        if record.resource != resource:
            raise OAuthError("invalid_target", "授权资源不匹配")
        if re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier) is None:
            raise OAuthError("invalid_grant", "PKCE verifier 格式不正确")
        expected = _pkce_challenge(code_verifier, record.code_challenge_method)
        if not hmac.compare_digest(expected, record.code_challenge):
            raise OAuthError("invalid_grant", "PKCE 校验失败")
        token, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self._lock:
            if self._codes.pop(code, None) is not record:
                raise OAuthError("invalid_grant", "授权码已被使用")
            self._tokens[token] = AccessToken(token, client_id, resource, time.time() + self.token_ttl, record.scope)
            self._refresh[refresh] = RefreshToken(client_id, resource, time.time() + max(self.token_ttl, 86_400) * 30, record.scope)
        return {"access_token": token, "refresh_token": refresh, "token_type": "Bearer",
                "expires_in": self.token_ttl, "scope": record.scope}

    def public_refresh_client(self, refresh_token: str) -> str:
        """公开客户端刷新可省略 client_id；从令牌绑定关系中识别它。"""
        with self._lock:
            entry = self._refresh.get(refresh_token)
            if entry is None or entry.expires_at < time.time():
                raise OAuthError("invalid_grant", "刷新令牌无效或已过期")
            client_id = entry.client_id
        client = self.get_client(client_id)
        if client is None or client.token_endpoint_auth_method != "none":
            raise OAuthError("invalid_client", "此客户端刷新时必须提供客户端凭据", status=401)
        return client_id

    def refresh_access_token(self, refresh_token: str, resource: str, *,
                             client_id: str | None = None, client_secret: str | None = None) -> dict[str, Any]:
        # HTTP 端点必须传入已认证的 client_id；省略只供本进程直接调用。
        if client_id is not None:
            self.authenticate_client(client_id, client_secret)
        with self._lock:
            entry = self._refresh.get(refresh_token)
            if entry is None or entry.expires_at < time.time():
                raise OAuthError("invalid_grant", "刷新令牌无效或已过期")
            if client_id is not None and entry.client_id != client_id:
                raise OAuthError("invalid_grant", "刷新令牌与客户端不匹配")
            if resource != entry.resource:
                raise OAuthError("invalid_target", "刷新资源不匹配")
            self._refresh.pop(refresh_token)
            token, new_refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            self._tokens[token] = AccessToken(token, entry.client_id, resource, time.time() + self.token_ttl, entry.scope)
            self._refresh[new_refresh] = RefreshToken(entry.client_id, resource, time.time() + max(self.token_ttl, 86_400) * 30, entry.scope)
        return {"access_token": token, "refresh_token": new_refresh, "token_type": "Bearer",
                "expires_in": self.token_ttl, "scope": entry.scope}

    def validate_token(self, token: str, resource: str | None = None) -> bool:
        with self._lock:
            record = self._tokens.get(token)
            if record is None:
                return False
            if record.expires_at < time.time():
                self._tokens.pop(token, None)
                return False
            return resource is None or record.resource == resource

    def active_tokens(self) -> int:
        with self._lock:
            return len(self._tokens)


def _pkce_challenge(verifier: str, method: str) -> str:
    if method == "plain":
        return verifier
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------
# 授权页面：用户在浏览器里输入启动脚本打印的那个口令
# --------------------------------------------------------------------------

_AUTHORIZE_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 授权</title>
<style>
  body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;background:#f6f7f9;
       display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;color:#1f2328}}
  .card{{background:#fff;border:1px solid #e3e6ea;border-radius:10px;padding:28px 32px;
        width:min(420px,92vw);box-shadow:0 1px 3px rgba(0,0,0,.04)}}
  h1{{font-size:17px;margin:0 0 6px}}
  p{{font-size:13px;color:#57606a;margin:0 0 18px;line-height:1.6}}
  code{{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:12px}}
  input{{width:100%;box-sizing:border-box;padding:9px 11px;border:1px solid #d0d7de;
        border-radius:6px;font-size:14px;margin-bottom:12px}}
  button{{width:100%;padding:9px;background:#1f883d;color:#fff;border:0;border-radius:6px;
         font-size:14px;cursor:pointer}}
  button:hover{{background:#1a7f37}}
  .err{{color:#cf222e;font-size:13px;margin-bottom:10px;min-height:18px}}
</style>
</head>
<body>
<form class="card" method="post" action="{action}">
  <h1>授权 {title} 访问工作区</h1>
  <p>工作区：<code>{workspace}</code><br>请输入启动 {title} 时终端里打印的<b>授权口令</b>。</p>
  <div class="err">{error}</div>
  <input type="hidden" name="client_id" value="{client_id}">
  <input type="hidden" name="redirect_uri" value="{redirect_uri}">
  <input type="hidden" name="state" value="{state}">
  <input type="hidden" name="code_challenge" value="{code_challenge}">
  <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
  <input type="hidden" name="resource" value="{resource}">
  <input type="hidden" name="scope" value="{scope}">
  <input type="password" name="password" placeholder="授权口令" autofocus required>
  <button type="submit">授权并继续</button>
</form>
</body>
</html>"""


def render_authorize_page(*, workspace: str, action: str, params: dict[str, str], error: str = "") -> str:
    fields = {
        "title": "ChatCode MCP",
        "workspace": html.escape(workspace),
        "action": html.escape(action),
        "error": html.escape(error),
        "client_id": html.escape(params.get("client_id", "")),
        "redirect_uri": html.escape(params.get("redirect_uri", "")),
        "state": html.escape(params.get("state", "")),
        "code_challenge": html.escape(params.get("code_challenge", "")),
        "code_challenge_method": html.escape(params.get("code_challenge_method", "S256")),
        "resource": html.escape(params.get("resource", "")),
        "scope": html.escape(params.get("scope", "")),
    }
    return _AUTHORIZE_PAGE.format(**fields)
