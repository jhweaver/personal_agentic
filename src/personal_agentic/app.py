"""The running agent: wires everything together and owns the main conversation.

Everything that wants the main conversation's attention (your messages from any client,
routines with context: main, notes from background work) goes through one queue, so
turns never interleave and the context stays in order.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from .agent.compaction import maybe_compact
from .agent.loop import run_loop
from .agent.session import Session
from .attention import AttentionManager
from .config import Config
from .events import EventBus
from .llm import (
    PRIORITY_BACKGROUND,
    PRIORITY_INTERACTIVE,
    PRIORITY_ROUTINE,
    Embedder,
    LLMClient,
    LLMGate,
)
from .mcp_servers import MCPManager
from .memory import Memory
from .prompts import MAIN_SYSTEM, SUBAGENT_SYSTEM
from .routines.manager import RoutineManager
from .runtime import IN_MAIN_TURN, detached_task
from .store import Store
from .tools.base import ToolContext, ToolRegistry
from .tools.builtin import register_builtin_tools
from .tools.custom import load_custom_tools
from .working_context import WorkingContext, diff_text

log = logging.getLogger(__name__)

# Messages from these sources are you typing; they count as replies to things waiting on you.
HUMAN_SOURCES = {"web", "terminal", "cli"}

HELP = """Commands (terminal or web):
  /think [message]   thinking on for chat (or just for this message)
  /fast [message]    thinking off for chat (or just for this message)
  /task NAME [input] run a task
  /run NAME          run a routine now
  /compact           summarize older conversation now
  /status            model, context size, servers, routines
  /help              this list
Terminal only: /approve [id] [note], /deny [id] [note], /stop, /wc, /quit"""


@dataclass
class Inbound:
    text: str
    source: str
    kind: str  # user | routine | note | internal
    future: asyncio.Future[str]
    thinking: bool | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class SubagentHandle:
    def __init__(self, sid: str, title: str):
        self.id = sid
        self.title = title
        self.status = "running"
        self.result = ""
        self.task: asyncio.Task[str] | None = None

    async def wait(self) -> str:
        assert self.task is not None
        try:
            return await asyncio.shield(self.task)
        except asyncio.CancelledError:
            if self.task.cancelled():
                return "(sub-agent was cancelled)"
            raise


class AgentApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(cfg.db_path)
        self.bus = EventBus(self.store)
        self.gate = LLMGate(cfg.llm.max_parallel)
        self.llm = LLMClient(cfg.llm, self.gate)
        self.embedder = Embedder(cfg.embeddings, cfg.llm.base_url, cfg.llm.api_key)
        self.memory = Memory(self.store, self.embedder)
        self.tools = ToolRegistry()
        self.wc = WorkingContext(cfg.resolve(cfg.paths.working_context), self.store)
        self.attention = AttentionManager(self)
        self.mcp = MCPManager(self)
        self.routines = RoutineManager(self)
        self.queue: asyncio.Queue[Inbound] = asyncio.Queue()
        self.subagents: dict[str, SubagentHandle] = {}
        self.main: Session | None = None
        self.main_thinking = cfg.thinking.chat
        self.current_turn: asyncio.Task[str] | None = None
        self.current_item: Inbound | None = None
        self.started_at = time.time()
        self._bg: list[asyncio.Task[Any]] = []

    # ---- lifecycle ----------------------------------------------------------------------------------
    async def start(self, *, scheduler: bool = True, mcp: bool = True) -> None:
        register_builtin_tools(self)
        custom = load_custom_tools(self)
        if custom:
            log.info("Loaded custom tools: %s", ", ".join(custom))
        self.main = Session(self, "main", "main", self.system_prompt("main"), title="Main")
        if self.main.preamble is None and not self.main.messages:
            self.main.set_preamble(self.build_preamble(self.main, None))
            self.store.kv_set("main", "wc_seen", self.wc.read())
        if mcp:
            await self.mcp.start()
        self.attention.start()
        self.routines.load()
        if scheduler:
            self.routines.start()
        self._bg.append(asyncio.create_task(self._main_worker(), name="main-worker"))
        self._bg.append(asyncio.create_task(self._housekeeping(), name="housekeeping"))
        self.bus.emit(
            "agent_started",
            {
                "model": self.cfg.llm.model,
                "tools": len(self.tools.all()),
                "routines": len(self.routines.defs),
            },
            source="system",
        )

    async def stop(self) -> None:
        await self.routines.stop()
        await self.attention.stop()
        pending: list[asyncio.Task[Any]] = [
            h.task for h in self.subagents.values() if h.task and not h.task.done()
        ]
        if self.current_turn and not self.current_turn.done():
            pending.append(self.current_turn)
        pending += self._bg
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.mcp.stop()
        await self.llm.aclose()
        await self.embedder.aclose()
        self.store.close()

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(600)
            try:
                n = await self.memory.backfill()
                if n:
                    log.info("Embedded %d memories saved earlier without embeddings", n)
            except Exception:
                log.exception("memory backfill failed")

    # ---- prompts ---------------------------------------------------------------------------------------
    def system_prompt(self, kind: str) -> str:
        main_path = self.cfg.resolve(self.cfg.paths.system_prompt)
        path = main_path if kind == "main" else main_path.parent / "subagent.md"
        text = path.read_text() if path.exists() else (MAIN_SYSTEM if kind == "main" else SUBAGENT_SYSTEM)
        values = {"user_name": self.cfg.agent.user_name, "timezone": self.cfg.workday.timezone}
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(values.get(m.group(1), m.group(0))), text).strip()

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.cfg.workday.timezone))

    def stamp(self, source: str) -> str:
        return f"[{self.now().strftime('%a %Y-%m-%d %H:%M %Z')} · via {source}]"

    def build_preamble(self, session: Session, summary: str | None) -> str:
        parts = [f"[Context carried over · {self.now().strftime('%a %Y-%m-%d %H:%M %Z')}]"]
        parts.append(
            "## Summary of earlier conversation\n" + (summary or "(This is the start of the conversation.)")
        )
        if session.kind == "main":
            parts.append("## Working context (snapshot)\n" + self.wc.read())
            self.store.kv_set("main", "wc_seen", self.wc.read())
        return "\n\n".join(parts)

    # ---- the main conversation ------------------------------------------------------------------------
    @property
    def main_busy(self) -> bool:
        return self.current_item is not None

    def submit(
        self,
        text: str,
        *,
        source: str,
        kind: str = "user",
        thinking: bool | None = None,
        meta: dict[str, Any] | None = None,
    ) -> asyncio.Future[str]:
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        if kind == "user" and self._quick_approval(text, source):
            fut.set_result("")
            return fut
        item = Inbound(text=text, source=source, kind=kind, future=fut, thinking=thinking, meta=meta or {})
        if kind == "user":
            if source in HUMAN_SOURCES and not text.startswith("/"):
                self.attention.on_user_message("main")
            self.bus.emit(
                "user_message",
                {"text": text, "queued": self.main_busy or not self.queue.empty()},
                session="main",
                source=source,
            )
        elif kind in ("note", "internal"):
            self.bus.emit("note", {"text": text}, session="main", source=source)
        elif kind == "routine":
            self.bus.emit("routine_prompt", {"text": text}, session="main", source=source)
        self.queue.put_nowait(item)
        return fut

    def _quick_approval(self, text: str, source: str) -> bool:
        """A bare "yes" / "no" answers the one approval the conversation is waiting on."""
        word = text.strip().lower().rstrip(".!")
        yes = word in {"y", "yes", "approve", "approved", "ok", "okay", "go", "go ahead", "do it", "send it"}
        no = word in {"n", "no", "deny", "denied", "don't", "dont", "cancel", "nope"}
        if not (yes or no):
            return False
        pending = self.store.open_attention(session="main", kind="approval")
        if len(pending) != 1 or not self.main_busy:
            return False
        self.bus.emit("user_message", {"text": text}, session="main", source=source)
        self.attention.resolve(pending[0]["id"], yes, "" if yes else "denied in chat", via=source)
        return True

    async def _main_worker(self) -> None:
        while True:
            item = await self.queue.get()
            self.current_item = item
            try:
                result = await self._handle(item)
                if not item.future.done():
                    item.future.set_result(result)
            except Exception as e:
                log.exception("main turn failed")
                self.bus.emit("error", {"text": f"{type(e).__name__}: {e}"}, session="main", source="system")
                if not item.future.done():
                    item.future.set_result(f"ERROR: {e}")
            finally:
                self.current_item = None
                self.current_turn = None
                self.bus.emit("status", {"state": "idle", "queue": self.queue.qsize()}, session="main")

    async def _handle(self, item: Inbound) -> str:
        assert self.main is not None
        if item.kind == "note":
            self.main.add_note(item.text)
            return ""
        if item.kind == "user" and item.text.startswith("/"):
            handled = await self._command(item)
            if handled is not None:
                return handled
        self.current_turn = asyncio.create_task(self._main_turn(item))
        await asyncio.wait({self.current_turn})
        if self.current_turn.cancelled():
            self.bus.emit("system_message", {"text": "Stopped."}, session="main", source="system")
            return "(stopped)"
        return self.current_turn.result()

    def stop_current(self) -> bool:
        if self.current_turn and not self.current_turn.done():
            self.current_turn.cancel()
            return True
        return False

    def _wc_change_note(self) -> str:
        seen = self.store.kv_get("main", "wc_seen")
        current = self.wc.read()
        if seen is None or seen == current:
            return ""
        d = diff_text(seen, current)
        if len(d) > 6000:
            return f"[The working context was changed outside this conversation. Current version:]\n{current}\n\n"
        return f"[The working context changed since you last saw it:]\n```diff\n{d}\n```\n\n"

    async def _main_turn(self, item: Inbound) -> str:
        assert self.main is not None
        IN_MAIN_TURN.set(True)  # this task's own context; sub-tasks inherit it
        text = item.text
        thinking = item.thinking
        if thinking is None:
            thinking = self.cfg.thinking.routines if item.kind == "routine" else self.main_thinking
        content = f"{self.stamp(item.source)}\n{self._wc_change_note()}{text}"
        self.main.repair()
        self.main.append({"role": "user", "content": content})
        ctx = ToolContext(self, "main", source=item.source, routine=item.meta.get("routine"), depth=0)
        priority = PRIORITY_INTERACTIVE if item.kind in ("user", "internal") else PRIORITY_ROUTINE
        self.store.upsert_session("main", "main", status="running")
        self.bus.emit(
            "status", {"state": "busy", "source": item.source, "thinking": thinking}, session="main"
        )
        try:
            return await run_loop(
                self,
                self.main,
                tools=self.tools.select(["*"], depth=0),
                thinking=thinking,
                priority=priority,
                max_steps=self.cfg.agent.max_steps,
                ctx=ctx,
            )
        finally:
            self.store.kv_set("main", "wc_seen", self.wc.read())
            self.store.upsert_session("main", "main", status="idle")

    async def _command(self, item: Inbound) -> str | None:
        """Slash commands. Returns None to treat the text as a normal message."""
        assert self.main is not None
        cmd, _, rest = item.text[1:].partition(" ")
        cmd, rest = cmd.lower(), rest.strip()

        def say(text: str) -> str:
            self.bus.emit("system_message", {"text": text}, session="main", source="system")
            return text

        if cmd in ("think", "fast"):
            on = cmd == "think"
            if rest:
                item.text, item.thinking = rest, on
                return None
            self.main_thinking = on
            return say(f"Thinking is now {'on' if on else 'off'} for chat.")
        if cmd == "help":
            return say(HELP)
        if cmd == "status":
            s = await self.status()
            return say(
                f"Model {s['model']} · context ~{s['context_tokens']:,}/{s['context_window']:,} tokens "
                f"(compacts at {s['compact_at']:,}) · thinking {'on' if s['thinking'] else 'off'}\n"
                + "MCP: "
                + (", ".join(f"{m['name']} {m['status']} ({m['tools']} tools)" for m in s["mcp"]) or "none")
                + "\n"
                + self.routines.describe()
            )
        if cmd == "compact":
            done = await maybe_compact(self, self.main, force=True)
            return say("Compacted the conversation." if done else "Nothing to compact yet.")
        if cmd in ("task", "run"):
            name, _, arg = rest.partition(" ")
            d = self.routines.defs.get(name)
            if not d:
                return say(f"No task or routine named {name!r}.\n{self.routines.describe()}")
            if cmd == "task" and d.python_fn is None and d.context == "main":
                item.text = f"[{d.kind.title()}: {d.name}]\n{d.render(self.cfg.agent.user_name, self.now(), arg.strip())}"
                item.kind = "routine"
                item.meta["routine"] = d.name
                if item.thinking is None and d.thinking is not None:
                    item.thinking = d.thinking
                return None
            self.routines.trigger(name, trigger="manual", input_text=arg.strip())
            return say(f"Started {d.kind} {name!r}. Its result will show up here when it's done.")
        return None

    # ---- sub-agents -----------------------------------------------------------------------------------------
    async def spawn_subagent(
        self,
        *,
        task: str,
        title: str | None = None,
        tools: list[str] | None = None,
        thinking: bool | None = None,
        background: bool = False,
        parent: str = "main",
        depth: int = 1,
        routine: str | None = None,
        max_steps: int | None = None,
    ) -> SubagentHandle:
        if depth > self.cfg.agent.max_subagent_depth:
            raise RuntimeError("Sub-agents can't start more sub-agents at this depth")
        sid = f"sub-{uuid.uuid4().hex[:8]}"
        title = title or (task.strip().splitlines()[0][:60] if task.strip() else "sub-agent")
        session = Session(
            self,
            sid,
            "subagent",
            self.system_prompt("subagent"),
            title=title,
            parent=parent,
            routine=routine,
            depth=depth,
        )
        self.store.upsert_session(
            sid, "subagent", status="running", meta={"task": task[:4000], "background": background}
        )
        handle = SubagentHandle(sid, title)
        self.subagents[sid] = handle
        if thinking is None:
            thinking = self.cfg.thinking.routines if routine else self.cfg.thinking.subagents
        priority = (
            PRIORITY_ROUTINE if routine else (PRIORITY_BACKGROUND if background else PRIORITY_INTERACTIVE)
        )
        source = f"routine:{routine}" if routine else "main agent"
        self.bus.emit(
            "subagent_started",
            {"id": sid, "title": title, "task": task[:4000], "routine": routine, "background": background},
            session=parent,
            source=source,
        )
        session.append({"role": "user", "content": f"{self.stamp(source)}\n{task}"})
        self.bus.emit("task", {"text": task}, session=sid, source=source)
        ctx = ToolContext(self, sid, source=source, routine=routine, depth=depth)

        async def runner() -> str:
            status, result = "done", ""
            try:
                result = await run_loop(
                    self,
                    session,
                    tools=self.tools.select(tools or ["*"], depth=depth),
                    thinking=thinking,
                    priority=priority,
                    max_steps=max_steps or self.cfg.agent.subagent_max_steps,
                    ctx=ctx,
                )
                return result
            except asyncio.CancelledError:
                status, result = "cancelled", "(cancelled)"
                raise
            except Exception as e:
                log.exception("sub-agent %s failed", sid)
                status, result = "error", f"ERROR: {type(e).__name__}: {e}"
                return result
            finally:
                handle.status, handle.result = status, result
                self.store.upsert_session(sid, "subagent", status=status, result=result)
                self.bus.emit(
                    "subagent_finished",
                    {"id": sid, "title": title, "status": status, "text": result[:6000], "routine": routine},
                    session=parent,
                    source=source,
                )
                if background and not routine and parent == "main":
                    self.submit(
                        f'[Background sub-agent {sid} "{title}" finished ({status}). Its result:]\n{result[:8000]}'
                        + ("\n[... cut; use agent_result for all of it]" if len(result) > 8000 else ""),
                        source="system",
                        kind="internal",
                    )

        handle.task = (
            detached_task(runner(), name=sid) if background else asyncio.create_task(runner(), name=sid)
        )
        return handle

    def cancel_subagent(self, sid: str) -> bool:
        h = self.subagents.get(sid)
        if h and h.task and not h.task.done():
            h.task.cancel()
            return True
        return False

    # ---- status ------------------------------------------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        window = await self.llm.context_window()
        assert self.main is not None
        schema_chars = len(json.dumps([t.openai_schema() for t in self.tools.select(["*"])]))
        return {
            "model": self.cfg.llm.model,
            "context_window": window,
            "context_tokens": self.main.estimate_tokens(schema_chars),
            "last_prompt_tokens": self.main.last_prompt_tokens,
            "compact_at": min(self.cfg.context.compact_at_tokens, int(window * 0.75)),
            "thinking": self.main_thinking,
            "busy": self.main_busy,
            "queue": self.queue.qsize(),
            "tools": len(self.tools.all()),
            "mcp": self.mcp.status(),
            "routines": self.routines.listing(),
            "open_attention": len(self.store.open_attention()),
            "uptime_s": int(time.time() - self.started_at),
            "user_name": self.cfg.agent.user_name,
        }
