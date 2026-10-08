"""Browser sign-in (OAuth) for remote MCP servers, like Claude Code's `/mcp` login.

`pa mcp login <server>` (or Sign in on the web page) opens your browser, catches the
redirect on http://localhost:<port>/callback, and saves the tokens in
~/.personal_agentic/oauth/<server>.json (readable only by you). After that the agent
refreshes them on its own; it never opens a browser by itself. A server that needs a
new sign-in shows as "needs sign-in" instead.

Servers that let apps register themselves (Dynamic Client Registration), like Atlassian
and most Glean setups, need no extra settings. Servers that don't, like Slack or Google,
need a client ID (and usually a secret) from an app you register with them:

    "slack": {
      "type": "http", "url": "https://mcp.slack.com/mcp",
      "oauth": {"clientId": "${SLACK_CLIENT_ID}", "clientSecret": "${SLACK_CLIENT_SECRET}",
                "callbackPort": 33418, "scopes": "search:read.public channels:history"}
    }

The app's allowed redirect URL must then be http://localhost:<callbackPort>/callback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)

log = logging.getLogger(__name__)

DEFAULT_CALLBACK_PORT = 33418
CLIENT_NAME = "personal-agentic"

OpenBrowser = Callable[[str], Awaitable[None] | None]


class LoginRequired(Exception):
    """The server wants a browser sign-in and we're not allowed to start one."""


@dataclass
class OAuthSettings:
    client_id: str | None = None
    client_secret: str | None = None
    callback_port: int = DEFAULT_CALLBACK_PORT
    scopes: str | None = None

    @property
    def redirect_uri(self) -> str:
        return f"http://localhost:{self.callback_port}/callback"


def uses_oauth(cfg: dict[str, Any]) -> bool:
    """HTTP/SSE servers sign in with OAuth unless they're given an Authorization header."""
    if cfg.get("oauth") is False:
        return False
    kind = (cfg.get("type") or cfg.get("transport") or ("stdio" if "command" in cfg else "http")).lower()
    if kind == "stdio":
        return False
    headers = {k.lower() for k in (cfg.get("headers") or {})}
    return "authorization" not in headers or bool(cfg.get("oauth"))


def oauth_settings(cfg: dict[str, Any]) -> OAuthSettings:
    raw = cfg.get("oauth")
    raw = raw if isinstance(raw, dict) else {}

    def pick(*keys: str) -> Any:
        for k in keys:
            if raw.get(k) not in (None, ""):
                return raw[k]
        return None

    scopes = pick("scopes", "scope")
    if isinstance(scopes, list):
        scopes = " ".join(scopes)
    port = pick("callbackPort", "callback_port")
    return OAuthSettings(
        client_id=pick("clientId", "client_id"),
        client_secret=pick("clientSecret", "client_secret"),
        callback_port=int(port) if port else DEFAULT_CALLBACK_PORT,
        scopes=scopes or None,
    )


# ---- token storage ------------------------------------------------------------------------------


class FileTokenStorage:
    """Tokens and the registered client for one server, in a 0600 JSON file."""

    def __init__(self, path: Path, static_client: OAuthClientInformationFull | None = None):
        self.path = path
        self.static_client = static_client

    def _load(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    def expires_at(self) -> float | None:
        return self._load().get("expires_at")

    def summary(self) -> dict[str, Any]:
        data = self._load()
        tokens = data.get("tokens") or {}
        return {
            "signed_in": bool(tokens.get("access_token")),
            "can_refresh": bool(tokens.get("refresh_token")),
            "expires_at": data.get("expires_at"),
            "saved_at": data.get("saved_at"),
            "registered_client": bool(data.get("client_info")),
        }

    def clear(self) -> bool:
        if self.path.exists():
            self.path.unlink()
            return True
        return False

    async def get_tokens(self) -> OAuthToken | None:
        tokens = self._load().get("tokens")
        return OAuthToken.model_validate(tokens) if tokens else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._load()
        old = data.get("tokens") or {}
        new = tokens.model_dump(mode="json", exclude_none=True)
        if "refresh_token" not in new and old.get("refresh_token"):
            new["refresh_token"] = old["refresh_token"]  # some servers don't rotate it
        data["tokens"] = new
        data["expires_at"] = time.time() + tokens.expires_in - 30 if tokens.expires_in else None
        data["saved_at"] = time.time()
        self._save(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        if self.static_client is not None:
            return self.static_client
        info = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        if self.static_client is not None:
            return  # configured client: nothing to remember (and never write the secret)
        data = self._load()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._save(data)


def token_path(data_dir: Path, server: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", server).lstrip(".") or "_"
    return data_dir / "oauth" / f"{safe}.json"


# ---- provider ------------------------------------------------------------------------------------


class AgentOAuthProvider(OAuthClientProvider):
    """The SDK's provider, plus: remembers token expiry across restarts (so a stale access
    token gets refreshed instead of forcing a new sign-in) and keeps configured scopes."""

    def __init__(self, *args: Any, storage: FileTokenStorage, fixed_scope: str | None, **kwargs: Any):
        super().__init__(*args, storage=storage, **kwargs)
        self._file_storage = storage
        self._fixed_scope = fixed_scope

    async def _initialize(self) -> None:
        await super()._initialize()
        expires_at = self._file_storage.expires_at()
        if expires_at:
            self.context.token_expiry_time = expires_at

    async def _perform_authorization_code_grant(self) -> tuple[str, str]:
        if self._fixed_scope:
            self.context.client_metadata.scope = self._fixed_scope
        return await super()._perform_authorization_code_grant()


def make_provider(
    *,
    name: str,
    url: str,
    cfg: dict[str, Any],
    data_dir: Path,
    redirect_handler: Callable[[str], Awaitable[None]],
    callback_handler: Callable[[], Awaitable[AuthorizationCodeResult]],
) -> AgentOAuthProvider:
    s = oauth_settings(cfg)
    static_client = None
    if s.client_id:
        static_client = OAuthClientInformationFull(
            client_id=s.client_id,
            client_secret=s.client_secret or None,
            redirect_uris=[s.redirect_uri],  # type: ignore[list-item]
            token_endpoint_auth_method="client_secret_post" if s.client_secret else "none",
            scope=s.scopes,
        )
    metadata = OAuthClientMetadata(
        client_name=CLIENT_NAME,
        redirect_uris=[s.redirect_uri],  # type: ignore[list-item]
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=s.scopes,
    )
    return AgentOAuthProvider(
        server_url=url,
        client_metadata=metadata,
        storage=FileTokenStorage(token_path(data_dir, name), static_client),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        fixed_scope=s.scopes,
    )


def background_provider(*, name: str, url: str, cfg: dict[str, Any], data_dir: Path) -> AgentOAuthProvider:
    """For the running agent: uses and refreshes saved tokens, never opens a browser."""

    async def refuse(_url: str) -> None:
        raise LoginRequired(name)

    async def no_callback() -> AuthorizationCodeResult:
        raise LoginRequired(name)

    return make_provider(
        name=name, url=url, cfg=cfg, data_dir=data_dir, redirect_handler=refuse, callback_handler=no_callback
    )


# ---- the local redirect catcher --------------------------------------------------------------------

_DONE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Signed in</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;margin:15vh auto;max-width:34em;padding:0 1em;color:#1C2733}}</style>
</head><body><h1>{title}</h1><p>{body}</p></body></html>"""


class CallbackServer:
    """Listens on localhost:<port> for the browser's redirect back from the sign-in page."""

    def __init__(self, port: int, path: str = "/callback"):
        self.port = port
        self.path = path
        self.result: asyncio.Future[AuthorizationCodeResult] | None = None
        self._servers: list[asyncio.Server] = []

    async def start(self) -> None:
        self.result = asyncio.get_running_loop().create_future()
        errors = []
        for host in ("127.0.0.1", "::1"):
            try:
                self._servers.append(await asyncio.start_server(self._handle, host, self.port))
            except OSError as e:
                errors.append(e)
        if not self._servers:
            raise RuntimeError(
                f"Can't listen on localhost:{self.port} for the sign-in redirect ({errors[0]}). "
                "Is another sign-in running? You can pick another port with oauth.callbackPort."
            )

    async def close(self) -> None:
        for s in self._servers:
            s.close()
            await s.wait_closed()
        self._servers.clear()

    async def wait(self, timeout: float) -> AuthorizationCodeResult:
        assert self.result is not None
        try:
            return await asyncio.wait_for(asyncio.shield(self.result), timeout)
        except TimeoutError as e:
            raise RuntimeError(f"No sign-in completed within {timeout:g} seconds") from e

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = (await asyncio.wait_for(reader.readline(), 10)).decode(errors="replace")
            while (await asyncio.wait_for(reader.readline(), 10)) not in (b"\r\n", b"\n", b""):
                pass
            parts = request_line.split()
            target = parts[1] if len(parts) > 1 else "/"
            url = urlsplit(target)
            if url.path != self.path:
                await self._respond(writer, 404, "Not found", "")
                return
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if "error" in q:
                msg = q.get("error_description") or q["error"]
                await self._respond(writer, 400, "Sign-in failed", f"The server said: {msg}")
                if self.result and not self.result.done():
                    self.result.set_exception(RuntimeError(f"Sign-in failed: {msg}"))
                return
            if "code" not in q:
                await self._respond(writer, 400, "Sign-in failed", "The redirect had no authorization code.")
                return
            await self._respond(writer, 200, "Signed in", "You can close this tab and go back to the agent.")
            if self.result and not self.result.done():
                self.result.set_result(
                    AuthorizationCodeResult(code=q["code"], state=q.get("state"), iss=q.get("iss"))
                )
        except (TimeoutError, ConnectionError):
            pass
        finally:
            writer.close()

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, status: int, title: str, body: str) -> None:
        html = _DONE_PAGE.format(title=title, body=body).encode()
        head = (
            f"HTTP/1.1 {status} OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(html)}\r\nConnection: close\r\n\r\n"
        ).encode()
        writer.write(head + html)
        await writer.drain()


async def default_open_browser(url: str) -> None:
    await asyncio.to_thread(webbrowser.open, url)


def interactive_provider(
    *,
    name: str,
    url: str,
    cfg: dict[str, Any],
    data_dir: Path,
    callback: CallbackServer,
    open_browser: OpenBrowser,
    timeout: float = 300,
) -> AgentOAuthProvider:
    async def redirect(auth_url: str) -> None:
        result = open_browser(auth_url)
        if asyncio.iscoroutine(result):
            await result

    async def wait_for_callback() -> AuthorizationCodeResult:
        return await callback.wait(timeout)

    return make_provider(
        name=name,
        url=url,
        cfg=cfg,
        data_dir=data_dir,
        redirect_handler=redirect,
        callback_handler=wait_for_callback,
    )


def find_exception(exc: BaseException, kind: type[BaseException]) -> BaseException | None:
    """Look through exception groups, causes and contexts for `kind`."""
    seen: set[int] = set()
    stack = [exc]
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, kind):
            return e
        if isinstance(e, BaseExceptionGroup):
            stack.extend(e.exceptions)
        if e.__cause__:
            stack.append(e.__cause__)
        if e.__context__:
            stack.append(e.__context__)
    return None
