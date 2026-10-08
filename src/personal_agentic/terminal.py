"""`pa chat`: the terminal client. It shows the same feed as the web page, live."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from typing import Any

import httpx
import websockets
from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from .config import DEFAULT_DATA_DIR


def _short(value: Any, n: int = 140) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


class TerminalChat:
    def __init__(self, url: str, source: str = "terminal", show_reasoning: bool = False):
        self.url = url.rstrip("/")
        self.source = source
        self.show_reasoning = show_reasoning
        self.console: Console | None = None
        self.state: dict[str, Any] = {
            "busy": False,
            "queue": 0,
            "tokens": None,
            "window": None,
            "connected": False,
        }
        self.sent: list[str] = []
        self.pending: dict[int, str] = {}  # open approvals/reviews: id -> title
        self.session: PromptSession | None = None

    # ---- rendering -------------------------------------------------------------------------------
    def out(self, renderable: Any) -> None:
        assert self.console is not None
        self.console.print(renderable)

    def render(self, ev: dict[str, Any], history: bool = False) -> None:
        kind, data, src = ev["kind"], ev.get("data") or {}, ev.get("source", "")
        if ev.get("session") not in (None, "main") and kind not in (
            "approval_request",
            "review_request",
            "attention_resolved",
            "subagent_started",
            "subagent_finished",
        ):
            return
        if kind == "user_message":
            if not history and src == self.source and data.get("text") in self.sent:
                self.sent.remove(data["text"])
                return
            self.out(Text.assemble((f"{src} › ", "bold cyan"), (data.get("text", ""), "cyan")))
        elif kind == "assistant_message":
            prefix = "agent (working) › " if data.get("interim") else "agent › "
            self.out(Text(prefix, style="bold green"))
            self.out(Markdown(data.get("text", "")))
        elif kind == "reasoning" and self.show_reasoning and not history:
            self.out(Text(_short(data.get("text", ""), 600), style="dim italic"))
        elif kind == "tool_call" and not history:
            self.out(Text(f"  → {data.get('name')} {_short(data.get('args', {}), 100)}", style="dim"))
        elif kind == "tool_result" and not history:
            mark = "✓" if data.get("ok", True) else "✗"
            self.out(Text(f"  {mark} {data.get('name')}: {_short(data.get('text', ''), 100)}", style="dim"))
        elif kind == "approval_request" and history:
            self.out(Text(f"approval #{data.get('attention_id')}: {data.get('title')}", style="yellow"))
        elif kind == "approval_request":
            aid = data.get("attention_id")
            self.pending[aid] = data.get("title", "")
            self.out(
                Panel(
                    f"[bold]{data.get('title')}[/bold]\n{_short(data.get('args', {}), 600)}\n\n"
                    f"/approve {aid} [note]   or   /deny {aid} [reason]   (or just answer yes / no)",
                    title=f"approval #{aid}",
                    border_style="yellow",
                )
            )
        elif kind == "review_request":
            aid = data.get("attention_id")
            self.pending[aid] = data.get("title", "")
            self.out(
                Text(
                    f"⏳ waiting on you (#{aid}): {data.get('title')}. Reply here to answer.", style="yellow"
                )
            )
        elif kind == "attention_resolved":
            self.pending.pop(data.get("attention_id"), None)
            self.out(Text(f"  #{data.get('attention_id')} {data.get('status')}", style="dim yellow"))
        elif kind == "notification":
            self.out(Text(f"🔔 {data.get('title')}: {data.get('text')}", style="magenta"))
        elif kind == "note":
            self.out(Text(f"note ({src}): {_short(data.get('text', ''), 400)}", style="dim"))
        elif kind == "script_output":
            style = "red" if data.get("level") == "error" or data.get("stream") == "stderr" else "dim green"
            for line in str(data.get("text", "")).splitlines()[-40:]:
                self.out(Text(f"[{src}] {line}", style=style))
        elif kind == "routine_started" and not history:
            self.out(Text(f"▶ routine {data.get('routine')} ({data.get('trigger')})", style="blue"))
        elif kind == "routine_finished":
            if data.get("quiet"):
                if not history:
                    self.out(Text(f"■ routine {data.get('routine')}: nothing new", style="dim blue"))
            else:
                self.out(Text(f"■ routine {data.get('routine')} {data.get('status')}", style="blue"))
        elif kind == "routine_skipped" and not history:
            self.out(Text(f"routine {data.get('routine')} skipped: {data.get('reason')}", style="dim"))
        elif kind == "subagent_started" and not history:
            self.out(Text(f"  ⧉ sub-agent {data.get('id')}: {data.get('title')}", style="dim blue"))
        elif kind == "subagent_finished" and not history:
            self.out(Text(f"  ⧉ sub-agent {data.get('id')} {data.get('status')}", style="dim blue"))
        elif kind == "system_message":
            self.out(Text(data.get("text", ""), style="cyan"))
        elif kind == "error":
            self.out(Text(f"error: {data.get('text')}", style="bold red"))
        elif kind == "compaction" and not history:
            self.out(
                Text(
                    f"context compacted: ~{data.get('before_tokens'):,} → ~{data.get('after_tokens'):,} tokens",
                    style="dim",
                )
            )
        elif kind == "mcp_status" and not history:
            self.out(
                Text(f"MCP {data.get('server')}: {data.get('status')} {data.get('error') or ''}", style="dim")
            )
        elif kind == "status":
            if data.get("state") == "busy":
                self.state["busy"] = True
            elif data.get("state") == "idle":
                self.state["busy"] = False
                self.state["queue"] = data.get("queue", 0)
            elif data.get("state") == "usage":
                self.state["tokens"] = data.get("prompt_tokens")
            if self.session and self.session.app.is_running:
                self.session.app.invalidate()

    def toolbar(self) -> HTML:
        s = self.state
        if not s["connected"]:
            return HTML("<b><style bg='ansired'> disconnected </style></b> reconnecting…")
        busy = "<b>working…</b>" if s["busy"] else "idle"
        tok = f" · ctx ~{s['tokens']:,}" if s.get("tokens") else ""
        win = f"/{s['window']:,}" if s.get("window") and s.get("tokens") else ""
        pend = f" · <b>{len(self.pending)} waiting on you</b>" if self.pending else ""
        return HTML(f"{busy}{tok}{win}{pend} · /help · /quit")

    # ---- main loop -------------------------------------------------------------------------------------
    async def run(self) -> None:
        hist = DEFAULT_DATA_DIR / "terminal_history"
        hist.parent.mkdir(parents=True, exist_ok=True)
        self.session = PromptSession(history=FileHistory(str(hist)), bottom_toolbar=self.toolbar)
        ws_url = self.url.replace("http://", "ws://").replace("https://", "wss://") + "/ws?client=terminal"
        with patch_stdout(raw=True):
            self.console = Console(
                file=sys.stdout, force_terminal=True, width=shutil.get_terminal_size().columns
            )
            send_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            conn = asyncio.create_task(self._connection(ws_url, send_q))
            try:
                while True:
                    try:
                        text = (await self.session.prompt_async(HTML("<b>› </b>"))).strip()
                    except (EOFError, KeyboardInterrupt):
                        break
                    if not text:
                        continue
                    if text in ("/quit", "/exit"):
                        break
                    msg = self._local_command(text)
                    if msg is None:
                        continue
                    await send_q.put(msg)
            finally:
                conn.cancel()

    def _local_command(self, text: str) -> dict[str, Any] | None:
        cmd, _, rest = text.partition(" ")
        if cmd in ("/approve", "/deny", "/a", "/d"):
            parts = rest.split(" ", 1)
            if parts and parts[0].isdigit():
                aid, note = int(parts[0]), (parts[1] if len(parts) > 1 else "")
            elif self.pending:
                aid, note = max(self.pending), rest
            else:
                self.out(Text("Nothing is waiting for approval.", style="yellow"))
                return None
            return {"type": "resolve", "id": aid, "approved": cmd in ("/approve", "/a"), "note": note}
        if cmd == "/stop":
            return {"type": "stop"}
        if cmd == "/wc":
            try:
                r = httpx.get(f"{self.url}/api/working-context", timeout=10)
                self.out(Markdown(r.json()["content"]))
            except httpx.HTTPError as e:
                self.out(Text(f"Couldn't fetch the working context: {e}", style="red"))
            return None
        self.sent.append(text)
        return {"type": "message", "text": text, "source": self.source}

    async def _connection(self, ws_url: str, send_q: asyncio.Queue[dict[str, Any]]) -> None:
        first = True
        while True:
            try:
                async with websockets.connect(ws_url, max_size=None, open_timeout=5) as ws:
                    hello = json.loads(await ws.recv())
                    st = hello.get("status", {})
                    self.state.update(
                        connected=True, window=st.get("context_window"), tokens=st.get("last_prompt_tokens")
                    )
                    if first:
                        self._banner(st)
                        await self._history()
                        first = False
                    else:
                        self.out(Text("reconnected", style="dim"))

                    async def sender() -> None:
                        while True:
                            await ws.send(json.dumps(await send_q.get()))

                    send_task = asyncio.create_task(sender())
                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            if msg.get("type") == "event":
                                self.render(msg["event"])
                            elif msg.get("type") == "error":
                                self.out(Text(msg.get("text", ""), style="red"))
                    finally:
                        send_task.cancel()
            except asyncio.CancelledError:
                raise
            except (OSError, websockets.WebSocketException) as e:
                if first:
                    self.out(
                        Text(
                            f"Can't reach the agent at {self.url} ({e}). Is `pa serve` running?", style="red"
                        )
                    )
            self.state["connected"] = False
            if self.session and self.session.app.is_running:
                self.session.app.invalidate()
            await asyncio.sleep(2)

    def _banner(self, st: dict[str, Any]) -> None:
        mcp = ", ".join(f"{m['name']} ({m['status']})" for m in st.get("mcp", [])) or "no MCP servers"
        window = st.get("context_window")
        window_s = f"{window:,}" if window else "?"
        self.out(
            Text.assemble(
                ("personal agent", "bold"),
                f" · {st.get('model')} · context {window_s} · {mcp}\n",
                ("Everything here also shows on the web page. /help for commands.", "dim"),
            )
        )

    async def _history(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(f"{self.url}/api/events", params={"session": "main", "limit": 40})
                events = r.json()
                r2 = await http.get(f"{self.url}/api/attention")
                for item in r2.json():
                    self.pending[item["id"]] = item["title"]
        except httpx.HTTPError:
            return
        if events:
            self.out(Text("── recent ──", style="dim"))
            for ev in events:
                self.render(ev, history=True)
            self.out(Text("── now ──", style="dim"))
        if self.pending:
            self.out(
                Text(
                    f"{len(self.pending)} item(s) waiting on you: "
                    + ", ".join(f"#{k}" for k in self.pending),
                    style="yellow",
                )
            )
