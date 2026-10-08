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

Each server's tools appear to the model as "<server>__<tool>". Servers that need an OAuth
browser sign-in aren't supported directly; see the README for bridging them over stdio.
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
        kind = (cfg.get("type") or cfg.get("transport") or ("stdio" if "command" in cfg else "http")).lower()
        if cfg.get("oauth") or cfg.get("auth"):
            raise ValueError(
                "OAuth sign-in isn't supported here; see README ('Servers that need a browser sign-in')"
            )
        if kind == "stdio":
            logs = self.manager.app.cfg.data_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            if self._errlog is None:
                self._errlog = open(logs / f"mcp-{self.name}.log", "a", buffering=1)  # noqa: SIM115, ASYNC230
            params = StdioServerParameters(
                command=cfg["command"],
                args=[str(a) for a in cfg.get("args", [])],
                env={
                    **{"PATH": os.environ.get("PATH", "")},
                    **{k: str(v) for k, v in cfg.get("env", {}).items()},
                },
                cwd=cfg.get("cwd"),
            )
            from mcp.client.stdio import stdio_client

            return stdio_client(params, errlog=self._errlog)
        if kind in ("http", "streamable-http", "streamable_http"):
            from mcp.client.streamable_http import streamable_http_client
            from mcp.shared._httpx_utils import create_mcp_http_client

            http = await stack.enter_async_context(create_mcp_http_client(headers=cfg.get("headers") or None))
            return streamable_http_client(cfg["url"], http_client=http)
        if kind == "sse":
            from mcp.client.sse import sse_client

            return sse_client(cfg["url"], headers=cfg.get("headers") or None)
        raise ValueError(f"Unknown MCP server type {kind!r}")

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
                self.error = _describe(e)
                self.status = "error"
                log.warning("MCP server %s: %s", self.name, self.error)
                self.manager.app.bus.emit(
                    "mcp_status",
                    {"server": self.name, "status": "error", "error": self.error},
                    source="system",
                )
            finally:
                self.client = None
                self.manager.app.tools.unregister_origin(self.origin)
                self.ready.set()  # don't block startup on a broken server
            if self._stop.is_set():
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
        await asyncio.gather(*(s.stop() for s in self.servers.values()), return_exceptions=True)
        self.servers.clear()

    async def reload(self) -> None:
        await self.stop()
        await self.start()

    def status(self) -> list[dict[str, Any]]:
        return [
            {"name": s.name, "status": s.status, "error": s.error, "tools": len(s.tool_names)}
            for s in self.servers.values()
        ]
