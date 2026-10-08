"""What a Python routine or task gets as `ctx`.

from personal_agentic.routines import routine

@routine(every="15m", window="workday", report="changes")
async def watch_something(ctx):
    raw = await ctx.call_tool("github__search_pull_requests", {"query": "is:open"})
    seen = ctx.state.get("seen", [])
    answer = await ctx.ask("Is anything here urgent? ...", tools=["slack__*"])
    await ctx.working_context.update("Plate", "append", "- [ ] ...")
    await ctx.notify("Something happened", via=["desktop"])
    ctx.state["seen"] = seen
    return "summary for the feed"   # or None / "NO_UPDATE" to stay quiet
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator, MutableMapping
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..llm import ToolCall
from ..memory import format_hits
from ..runtime import IN_MAIN_TURN
from ..tools.base import ToolContext

if TYPE_CHECKING:
    from ..app import AgentApp
    from .defs import RoutineDef


class State(MutableMapping[str, Any]):
    """Dict-like state that persists between runs (SQLite-backed, JSON values)."""

    def __init__(self, app: AgentApp, namespace: str):
        self._store = app.store
        self._ns = namespace

    def __getitem__(self, key: str) -> Any:
        sentinel = object()
        value = self._store.kv_get(self._ns, key, sentinel)
        if value is sentinel:
            raise KeyError(key)
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        self._store.kv_set(self._ns, key, value)

    def __delitem__(self, key: str) -> None:
        self._store.kv_delete(self._ns, key)

    def __iter__(self) -> Iterator[str]:
        return iter(self._store.kv_all(self._ns))

    def __len__(self) -> int:
        return len(self._store.kv_all(self._ns))


class _WorkingContextAPI:
    def __init__(self, app: AgentApp, author: str):
        self._app = app
        self._author = author

    def read(self) -> str:
        return self._app.wc.read()

    async def update(self, section: str, action: str, text: str = "") -> str:
        return self._app.wc.update(section, action, text, author=self._author)

    async def write(self, content: str) -> None:
        self._app.wc.write(content, author=self._author)


class RoutineContext:
    def __init__(self, app: AgentApp, defn: RoutineDef, session_id: str, trigger: str, input_text: str = ""):
        self.app = app
        self.routine = defn
        self.name = defn.name
        self.session_id = session_id
        self.trigger = trigger
        self.input = input_text
        self.vars = dict(defn.vars)
        self.state = State(app, f"routine:{defn.name}")
        self.working_context = _WorkingContextAPI(app, f"routine:{defn.name}")
        self.log = logging.getLogger(f"routine.{defn.name}")

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.app.cfg.workday.timezone))

    def emit(self, text: str) -> None:
        """Post a progress line to the web feed."""
        self.app.bus.emit(
            "routine_log",
            {"routine": self.name, "text": text},
            session=self.session_id,
            source=f"routine:{self.name}",
        )

    async def call_tool(self, name: str, args: dict[str, Any] | None = None) -> str:
        """Call any tool by name (MCP tools are '<server>__<tool>'). Approval rules still apply."""
        from ..agent.loop import execute_tool_call

        spec = self.app.tools.get(name)
        tool_map = {name: spec} if spec else {}
        ctx = ToolContext(
            self.app, self.session_id, source=f"routine:{self.name}", routine=self.name, depth=1
        )
        call = ToolCall(id=f"py_{uuid.uuid4().hex[:10]}", name=name, arguments=json.dumps(args or {}))
        return await execute_tool_call(self.app, call, tool_map, ctx)

    async def ask(
        self,
        prompt: str,
        *,
        tools: list[str] | None = None,
        thinking: bool | None = None,
        title: str | None = None,
    ) -> str:
        """Run a sub-agent on `prompt` (its own context) and return its answer."""
        handle = await self.app.spawn_subagent(
            task=prompt,
            title=title or f"{self.name}: {prompt[:40]}",
            tools=tools,
            thinking=self.routine.thinking if thinking is None else thinking,
            background=False,
            parent=self.session_id,
            depth=1,
            routine=self.name,
        )
        return await handle.wait()

    async def ask_main(self, prompt: str, *, thinking: bool | None = None) -> str:
        """Put `prompt` into the main conversation and return the agent's reply."""
        if IN_MAIN_TURN.get():
            raise RuntimeError(
                "ask_main() can't be used here: the main conversation is waiting on this task. "
                "Use ctx.ask() for a sub-agent instead."
            )
        fut = self.app.submit(
            f"[Routine: {self.name}]\n{prompt}",
            source=f"routine:{self.name}",
            kind="routine",
            thinking=thinking,
            meta={"routine": self.name},
        )
        return await fut

    async def notify(
        self,
        text: str,
        *,
        title: str | None = None,
        via: list[str] | tuple[str, ...] = (),
        needs_reply: bool = False,
    ) -> None:
        from ..notify import desktop_notification, send_imessage

        title = title or self.name
        self.app.bus.emit(
            "notification",
            {"title": title, "text": text, "via": list(via)},
            session="main",
            source=f"routine:{self.name}",
        )
        if "desktop" in via:
            await desktop_notification(title, text)
        if "imessage" in via:
            await send_imessage(self.app.cfg.notify.imessage_self or "", f"{title}: {text}")
        if needs_reply:
            self.app.submit(
                f"[Routine {self.name} needs a reply]\n{text}", source=f"routine:{self.name}", kind="note"
            )
            self.app.attention.open_review("main", title, text, self.routine.escalate)

    async def remember(self, text: str) -> int:
        return await self.app.memory.add(text, kind="routine", meta={"routine": self.name})

    async def recall(self, query: str, k: int = 5) -> str:
        return format_hits(await self.app.memory.search(query, k=k))
