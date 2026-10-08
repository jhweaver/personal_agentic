"""Connections to your MCP servers.

mcp.json uses the same shape as Claude Desktop / Claude Code, so configs can be pasted:

    {
      "mcpServers": {
        "slack":   {"command": "npx", "args": ["-y", "some-slack-mcp"], "env": {"SLACK_TOKEN": "${SLACK_TOKEN}"}},
        "company": {"type": "http", "url": "https://mcp.example.internal/mcp",
                    "headers": {"Authorization": "Bearer ${COMPANY_TOKEN}"}}
      }
    }

Extra keys this agent understands, per server:
    "disabled": true                 skip it
    "include_tools": ["search_*"]    only expose matching tools
    "exclude_tools": ["delete_*"]    hide matching tools
    "policy": {"send_*": "ask", "*": "allow"}   approval rules on the server's own tool names
    "timeout": 120                   seconds per tool call

    "oauth": {...}                   sign-in settings (see oauth.py); same keys as Claude Code
    "headersHelper": "cmd"           shell command printing a JSON object of extra headers

Each server's tools appear to the model as "<server>__<tool>". HTTP servers without an
Authorization header sign in with OAuth: run `pa mcp login <server>` once.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import os
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from mcp import Client, StdioServerParameters

from .oauth import (
    CallbackServer,
    FileTokenStorage,
    LoginRequired,
    OpenBrowser,
    background_provider,
    find_exception,
    interactive_provider,
    oauth_settings,
    token_path,
    uses_oauth,
)
from .tools.base import ToolContext, ToolSpec, safe_tool_name

if TYPE_CHECKING:
    from .app import AgentApp

log = logging.getLogger(__name__)

PING_EVERY = 30.0
RECONNECT_FIRST = 5.0

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _VAR.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def load_mcp_config(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text() or "{}")
    servers = raw.get("mcpServers", raw.get("servers", {}))
    return {name: expand_env(cfg) for name, cfg in servers.items()}


def server_kind(cfg: dict[str, Any]) -> str:
    kind = (cfg.get("type") or cfg.get("transport") or ("stdio" if "command" in cfg else "http")).lower()
    return "http" if kind in ("streamable-http", "streamable_http", "http") else kind


async def helper_headers(cfg: dict[str, Any]) -> dict[str, str]:
    """Run `headersHelper` (a shell command that prints a JSON object of headers)."""
    cmd = cfg.get("headersHelper")
    if not cmd:
        return {}
    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), 20)
    except TimeoutError as e:
        proc.kill()
        raise RuntimeError("headersHelper took longer than 20 seconds") from e
    if proc.returncode != 0:
        raise RuntimeError(f"headersHelper failed: {err.decode(errors='replace').strip()[:300]}")
    data = json.loads(out or b"{}")
    if not isinstance(data, dict):
        raise RuntimeError("headersHelper must print a JSON object")
    return {str(k): str(v) for k, v in data.items()}


async def open_transport(
    stack: AsyncExitStack, cfg: dict[str, Any], *, auth: Any = None, errlog: TextIO | None = None
) -> Any:
    kind = server_kind(cfg)
    if kind == "stdio":
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=cfg["command"],
            args=[str(a) for a in cfg.get("args", [])],
            env={
                **{"PATH": os.environ.get("PATH", "")},
                **{k: str(v) for k, v in cfg.get("env", {}).items()},
            },
            cwd=cfg.get("cwd"),
        )
        return stdio_client(params, errlog=errlog) if errlog else stdio_client(params)
    headers = {**(cfg.get("headers") or {}), **(await helper_headers(cfg))}
    if kind == "http":
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        http = await stack.enter_async_context(create_mcp_http_client(headers=headers or None, auth=auth))
        return streamable_http_client(cfg["url"], http_client=http)
    if kind == "sse":
        from mcp.client.sse import sse_client

        return sse_client(cfg["url"], headers=headers or None, auth=auth)
    raise ValueError(f"Unsupported MCP server type {kind!r} (use stdio, http or sse)")


async def sign_in(
    name: str, cfg: dict[str, Any], data_dir: Path, open_browser: OpenBrowser, timeout: float = 300
) -> int:
    """Browser sign-in for one server. Returns how many tools it offers once connected."""
    if server_kind(cfg) not in ("http", "sse"):
        raise ValueError(f"{name} is a {server_kind(cfg)} server; only http/sse servers sign in")
    if not uses_oauth(cfg):
        raise ValueError(f"{name} has an Authorization header in mcp.json, so it doesn't use sign-in")
    callback = CallbackServer(oauth_settings(cfg).callback_port)
    await callback.start()
    try:
        provider = interactive_provider(
            name=name,
            url=cfg["url"],
            cfg=cfg,
            data_dir=data_dir,
            callback=callback,
            open_browser=open_browser,
            timeout=timeout,
        )
        try:
            async with AsyncExitStack() as stack:
                transport = await open_transport(stack, cfg, auth=provider)
                client = await stack.enter_async_context(Client(transport, read_timeout_seconds=60))
                listing = await client.list_tools()
                return len(listing.tools)
        except BaseExceptionGroup as group:
            raise root_error(group) from group
    finally:
        await callback.close()


def root_error(exc: BaseException) -> BaseException:
    """The first real error inside (possibly nested) exception groups."""
    if isinstance(exc, BaseExceptionGroup):
        inner = [root_error(e) for e in exc.exceptions]
        real = [e for e in inner if not isinstance(e, asyncio.CancelledError | KeyboardInterrupt)]
        return (real or inner or [exc])[0]
    return exc


def format_tool_result(result: Any, max_chars: int) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        btype = getattr(block, "type", "")
        if btype == "text":
            parts.append(block.text)
        elif btype == "resource":
            res = getattr(block, "resource", None)
            text = getattr(res, "text", None)
            parts.append(text if text else f"[resource {getattr(res, 'uri', '')}]")
        elif btype == "resource_link":
            parts.append(f"[link {getattr(block, 'uri', '')} {getattr(block, 'name', '')}]")
        else:
            parts.append(f"[{btype or 'non-text'} content omitted]")
    structured = getattr(result, "structured_content", None)
    if structured and not parts:
        parts.append(json.dumps(structured, indent=1, default=str))
    text = "\n".join(parts) if parts else "(no output)"
    if getattr(result, "is_error", False):
        text = f"ERROR from tool: {text}"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[... cut: {len(text) - max_chars} more characters]"
    return text


class MCPServer:
    def __init__(self, manager: MCPManager, name: str, cfg: dict[str, Any]):
        self.manager = manager
        self.name = name
        self.cfg = cfg
        self.client: Client | None = None
        self.status = "starting"
        self.error: str | None = None
        self.tool_names: list[str] = []
        self.ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._errlog: TextIO | None = None
        self._tools_sig = ""

    @property
    def origin(self) -> str:
        return f"mcp:{self.name}"

    def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name=f"mcp:{self.name}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.manager.app.tools.unregister_origin(self.origin)
        if self._errlog:
            self._errlog.close()

    async def _transport(self, stack: AsyncExitStack) -> Any:
        cfg = self.cfg
        if server_kind(cfg) == "stdio":
            logs = self.manager.app.cfg.data_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            if self._errlog is None:
                self._errlog = open(logs / f"mcp-{self.name}.log", "a", buffering=1)  # noqa: SIM115, ASYNC230
            return await open_transport(stack, cfg, errlog=self._errlog)
        auth = None
        if uses_oauth(cfg):
            auth = background_provider(
                name=self.name, url=cfg["url"], cfg=cfg, data_dir=self.manager.app.cfg.data_dir
            )
        return await open_transport(stack, cfg, auth=auth)

    async def _supervise(self) -> None:
        backoff = RECONNECT_FIRST
        while not self._stop.is_set():
            try:
                async with AsyncExitStack() as stack:
                    transport = await self._transport(stack)
                    timeout = float(self.cfg.get("timeout", 120))
                    client = await stack.enter_async_context(Client(transport, read_timeout_seconds=timeout))
                    self.client = client
                    await self._register_tools(client)
                    self.status, self.error = "connected", None
                    self.ready.set()
                    backoff = RECONNECT_FIRST
                    self.manager.app.bus.emit(
                        "mcp_status",
                        {"server": self.name, "status": "connected", "tools": len(self.tool_names)},
                        source="system",
                    )
                    await self._watch(client)
            except asyncio.CancelledError:
                raise
            except BaseException as e:  # noqa: BLE001 - anyio may raise ExceptionGroup
                needs_login = find_exception(e, LoginRequired) is not None
                if needs_login:
                    self.status = "needs_login"
                    self.error = (
                        f"Not signed in. Run `pa mcp login {self.name}` or use Sign in on the web page."
                    )
                else:
                    self.status, self.error = "error", _describe(e)
                log.warning("MCP server %s: %s", self.name, self.error)
                self.manager.app.bus.emit(
                    "mcp_status",
                    {"server": self.name, "status": self.status, "error": self.error},
                    source="system",
                )
            finally:
                self.client = None
                self.manager.app.tools.unregister_origin(self.origin)
                self.ready.set()  # don't block startup on a broken server
            if self._stop.is_set():
                break
            if self.status == "needs_login":
                await self._stop.wait()  # signing in restarts this server
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)

    async def _watch(self, client: Client) -> None:
        """Wait for stop(). Meanwhile re-list tools now and then: that notices a server that
        died (so the reconnect loop kicks in) and picks up tools it added or removed.
        (MCP's ping was dropped in the 2026-07-28 protocol, so listing doubles as the check.)"""
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=PING_EVERY)
                return
            except TimeoutError:
                pass
            try:
                await asyncio.wait_for(self._register_tools(client, refresh=True), timeout=30)
            except Exception as e:
                raise ConnectionError(f"server stopped responding ({_describe(e)})") from e

    async def _register_tools(self, client: Client, refresh: bool = False) -> None:
        tools = []
        cursor = None
        for _ in range(50):
            page = await client.list_tools(cursor=cursor, cache_mode="refresh" if refresh else "use")
            tools.extend(page.tools)
            cursor = page.next_cursor
            if not cursor:
                break
        sig = json.dumps(
            sorted((t.name, t.description or "", t.input_schema or {}) for t in tools),
            sort_keys=True,
            default=str,
        )
        if refresh and sig == self._tools_sig:
            return  # unchanged; leave the registry alone
        self._tools_sig = sig
        include = self.cfg.get("include_tools") or ["*"]
        exclude = self.cfg.get("exclude_tools") or []
        rules: dict[str, str] = self.cfg.get("policy") or {}
        registry = self.manager.app.tools
        registry.unregister_origin(self.origin)
        self.tool_names = []
        for t in tools:
            if not any(fnmatch.fnmatch(t.name, p) for p in include) or any(
                fnmatch.fnmatch(t.name, p) for p in exclude
            ):
                continue
            ann = t.annotations
            policy = next((a for pat, a in rules.items() if fnmatch.fnmatch(t.name, pat)), None)
            spec = ToolSpec(
                name=safe_tool_name(f"{self.name}__{t.name}"),
                description=(t.description or t.title or t.name).strip(),
                parameters=t.input_schema or {"type": "object", "properties": {}},
                handler=self._make_handler(t.name),
                origin=self.origin,
                read_only=getattr(ann, "read_only_hint", None) if ann else None,
                destructive=getattr(ann, "destructive_hint", None) if ann else None,
                policy=policy,  # type: ignore[arg-type]
                extra={"mcp_name": t.name, "server": self.name},
            )
            registry.register(spec)
            self.tool_names.append(spec.name)

    def _make_handler(self, tool_name: str):
        async def handler(args: dict[str, Any], ctx: ToolContext) -> str:
            if self.client is None:
                return f"ERROR: MCP server {self.name!r} is not connected ({self.error or self.status})."
            result = await self.client.call_tool(tool_name, args)
            return format_tool_result(result, ctx.app.cfg.context.max_tool_result_chars)

        return handler


def _describe(e: BaseException) -> str:
    if isinstance(e, BaseExceptionGroup):
        return "; ".join(_describe(x) for x in e.exceptions)
    return f"{type(e).__name__}: {e}"


class MCPManager:
    def __init__(self, app: AgentApp):
        self.app = app
        self.servers: dict[str, MCPServer] = {}
        self._logins: dict[str, asyncio.Task[int]] = {}

    @property
    def config_path(self) -> Path:
        return self.app.cfg.resolve(self.app.cfg.paths.mcp_config)

    async def start(self, wait: float = 20.0) -> None:
        for name, cfg in load_mcp_config(self.config_path).items():
            if cfg.get("disabled"):
                continue
            server = MCPServer(self, name, cfg)
            self.servers[name] = server
            server.start()
        if self.servers and wait:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(s.ready.wait() for s in self.servers.values())), timeout=wait
                )
            except TimeoutError:
                log.warning("Some MCP servers are still starting; continuing without waiting")

    async def stop(self) -> None:
        for t in self._logins.values():
            t.cancel()
        await asyncio.gather(*(s.stop() for s in self.servers.values()), return_exceptions=True)
        self.servers.clear()

    async def reload(self) -> None:
        await self.stop()
        await self.start()

    async def restart(self, name: str, wait: float = 20.0) -> None:
        """Re-read mcp.json and reconnect one server (e.g. after signing in)."""
        old = self.servers.pop(name, None)
        if old:
            await old.stop()
        cfg = load_mcp_config(self.config_path).get(name)
        if not cfg or cfg.get("disabled"):
            return
        server = MCPServer(self, name, cfg)
        self.servers[name] = server
        server.start()
        try:
            await asyncio.wait_for(server.ready.wait(), timeout=wait)
        except TimeoutError:
            pass

    async def start_login(self, name: str) -> dict[str, Any]:
        """Start a sign-in from the web page: returns the URL for the browser to open."""
        cfg = load_mcp_config(self.config_path).get(name)
        if cfg is None:
            raise KeyError(f"No MCP server named {name!r} in mcp.json")
        previous = self._logins.pop(name, None)
        if previous and not previous.done():
            previous.cancel()
            await asyncio.gather(previous, return_exceptions=True)
        url_ready: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def hand_over(url: str) -> None:
            if not url_ready.done():
                url_ready.set_result(url)

        async def run() -> int:
            count = await sign_in(name, cfg, self.app.cfg.data_dir, hand_over)
            self.app.bus.emit("mcp_status", {"server": name, "status": "signed_in"}, source="system")
            await self.restart(name)
            return count

        task = asyncio.create_task(run(), name=f"login:{name}")
        self._logins[name] = task

        def report(t: asyncio.Task[int]) -> None:
            if not t.cancelled() and t.exception() is not None:
                self.app.bus.emit(
                    "error",
                    {"text": f"Sign-in to {name} failed: {_describe(t.exception())}"},
                    source="system",
                )

        task.add_done_callback(report)
        done, _ = await asyncio.wait({url_ready, task}, timeout=30, return_when=asyncio.FIRST_COMPLETED)
        if url_ready in done:
            return {"url": url_ready.result()}
        if task in done:
            return {"done": True, "tools": task.result()}
        raise TimeoutError("The server didn't start a sign-in within 30 seconds")

    def status(self) -> list[dict[str, Any]]:
        out = []
        for s in self.servers.values():
            oauth = uses_oauth(s.cfg)
            out.append(
                {
                    "name": s.name,
                    "status": s.status,
                    "error": s.error,
                    "tools": len(s.tool_names),
                    "auth": "oauth" if oauth else ("headers" if s.cfg.get("headers") else None),
                    "signed_in": FileTokenStorage(token_path(self.app.cfg.data_dir, s.name)).summary()[
                        "signed_in"
                    ]
                    if oauth
                    else None,
                }
            )
        return out
