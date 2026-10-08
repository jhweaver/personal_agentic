"""Tools the agent always has: reaching you, the working context, memory, sub-agents."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from ..memory import format_hits
from ..notify import desktop_notification, send_imessage
from .base import Action, ToolContext, ToolSpec

if TYPE_CHECKING:
    from ..app import AgentApp


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _is_self(app: AgentApp, handle: str) -> bool:
    me = (app.cfg.notify.imessage_self or "").strip().lower()
    h = handle.strip().lower()
    digits = lambda s: "".join(c for c in s if c.isdigit())  # noqa: E731
    return h in ("me", "self", "myself") or (
        bool(me) and (h == me or (digits(h) and digits(h)[-10:] == digits(me)[-10:]))
    )


def register_builtin_tools(app: AgentApp) -> None:
    reg = app.tools

    # ---- reaching the user ------------------------------------------------------------------
    async def notify_user(args: dict[str, Any], ctx: ToolContext) -> str:
        message = args["message"]
        title = args.get("title") or "Agent"
        via = args.get("via") or []
        app.bus.emit("notification", {"title": title, "text": message, "via": via}, session=ctx.session)
        sent = ["web"]
        for channel in via:
            if channel == "desktop":
                await desktop_notification(title, message)
                sent.append("desktop")
            elif channel == "imessage":
                await send_imessage(app.cfg.notify.imessage_self or "", f"{title}: {message}")
                sent.append("imessage")
        if args.get("needs_reply"):
            app.attention.open_review("main", title, message)
            sent.append("waiting for reply (will escalate)")
        return f"Delivered via {', '.join(sent)}."

    reg.register(
        ToolSpec(
            name="notify_user",
            description=(
                "Tell the user something outside the current reply: always shows on the web page; "
                "optionally also as a macOS desktop alert and/or an iMessage to them. Set needs_reply "
                "when you need an answer: it will escalate (desktop, then iMessage) if they don't respond."
            ),
            parameters=_obj(
                {
                    "message": {"type": "string"},
                    "title": {"type": "string", "description": "Short title"},
                    "via": {"type": "array", "items": {"enum": ["desktop", "imessage"]}},
                    "needs_reply": {"type": "boolean"},
                },
                ["message"],
            ),
            handler=notify_user,
            policy="allow",
        )
    )

    async def imessage(args: dict[str, Any], ctx: ToolContext) -> str:
        to = args["to"]
        handle = app.cfg.notify.imessage_self if _is_self(app, to) else to
        result = await send_imessage(handle or "", args["text"])
        app.bus.emit(
            "notification",
            {"title": f"iMessage to {to}", "text": args["text"], "via": ["imessage"]},
            session=ctx.session,
        )
        return f"iMessage {result}."

    def imessage_policy(args: dict[str, Any], ctx: ToolContext) -> Action:
        return "allow" if _is_self(app, str(args.get("to", ""))) else "ask"

    reg.register(
        ToolSpec(
            name="send_imessage",
            description=(
                "Send an iMessage from the user's Mac. `to` is a phone number or Apple ID email, or "
                "'me' for the user themself. Messages to anyone else need the user's approval."
            ),
            parameters=_obj({"to": {"type": "string"}, "text": {"type": "string"}}, ["to", "text"]),
            handler=imessage,
            policy=imessage_policy,
        )
    )

    async def desktop(args: dict[str, Any], ctx: ToolContext) -> str:
        result = await desktop_notification(
            args.get("title") or "Agent", args["message"], args.get("subtitle", "")
        )
        app.bus.emit(
            "notification",
            {"title": args.get("title") or "Agent", "text": args["message"], "via": ["desktop"]},
            session=ctx.session,
        )
        return f"Desktop notification {result}."

    reg.register(
        ToolSpec(
            name="desktop_alert",
            description="Show a macOS notification on the user's screen.",
            parameters=_obj(
                {"title": {"type": "string"}, "message": {"type": "string"}, "subtitle": {"type": "string"}},
                ["message"],
            ),
            handler=desktop,
            policy="allow",
        )
    )

    # ---- working context ------------------------------------------------------------------------
    async def wc_read(args: dict[str, Any], ctx: ToolContext) -> str:
        return app.wc.read()

    async def wc_update(args: dict[str, Any], ctx: ToolContext) -> str:
        return app.wc.update(
            args["section"], args["action"], args.get("text", ""), author=f"agent:{ctx.session}"
        )

    async def wc_write(args: dict[str, Any], ctx: ToolContext) -> str:
        app.wc.write(args["content"], author=f"agent:{ctx.session}")
        return "Working context replaced (previous version saved)."

    reg.register(
        ToolSpec(
            name="working_context_read",
            description="Read the user's working context (their plate, in-progress work, waiting-on, daily log, notes).",
            parameters=_obj({}),
            handler=wc_read,
            policy="allow",
        )
    )
    reg.register(
        ToolSpec(
            name="working_context_update",
            description=(
                "Edit one section of the working context. Sections: Plate, In progress, Waiting on others, "
                "Daily log, Notes (new ones are created on append). Actions: append | prepend | replace "
                "(whole section) | remove (lines containing text) | check (tick '- [ ]' lines containing text). "
                "Plate items look like '- [ ] what (source, YYYY-MM-DD)'."
            ),
            parameters=_obj(
                {
                    "section": {"type": "string"},
                    "action": {"enum": ["append", "prepend", "replace", "remove", "check"]},
                    "text": {"type": "string"},
                },
                ["section", "action"],
            ),
            handler=wc_update,
            policy="allow",
        )
    )
    reg.register(
        ToolSpec(
            name="working_context_write",
            description="Replace the whole working context file. Prefer working_context_update for small edits.",
            parameters=_obj({"content": {"type": "string"}}, ["content"]),
            handler=wc_write,
            policy="allow",
        )
    )

    # ---- memory --------------------------------------------------------------------------------------
    async def remember(args: dict[str, Any], ctx: ToolContext) -> str:
        text = args["text"]
        if args.get("tags"):
            text = f"{text}\n(tags: {', '.join(args['tags'])})"
        mid = await app.memory.add(text, kind="note", meta={"session": ctx.session})
        return f"Saved as memory #{mid}."

    async def recall(args: dict[str, Any], ctx: ToolContext) -> str:
        kinds = args.get("kinds") or None
        hits = await app.memory.search(args["query"], k=int(args.get("k", 8)), kinds=kinds)
        return format_hits(hits)

    reg.register(
        ToolSpec(
            name="remember",
            description="Save a durable fact, preference or decision to long-term memory.",
            parameters=_obj(
                {"text": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}}, ["text"]
            ),
            handler=remember,
            policy="allow",
        )
    )
    reg.register(
        ToolSpec(
            name="recall",
            description=(
                "Search long-term memory: saved notes, routine outputs, and older conversation that was "
                "compacted out of the current context."
            ),
            parameters=_obj(
                {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "description": "Number of results (default 8)"},
                    "kinds": {"type": "array", "items": {"enum": ["note", "archive", "routine"]}},
                },
                ["query"],
            ),
            handler=recall,
            policy="allow",
        )
    )

    # ---- sub-agents ----------------------------------------------------------------------------------
    async def spawn(args: dict[str, Any], ctx: ToolContext) -> str:
        handle = await app.spawn_subagent(
            task=args["task"],
            title=args.get("title"),
            tools=args.get("tools"),
            thinking=args.get("thinking"),
            background=bool(args.get("background", False)),
            parent=ctx.session,
            depth=ctx.depth + 1,
            routine=ctx.routine,
        )
        if args.get("background"):
            return (
                f"Sub-agent {handle.id} started in the background. You'll get a note when it finishes; "
                f"use agent_result('{handle.id}') to read its result."
            )
        try:
            result = await handle.wait()
        except asyncio.CancelledError:
            app.cancel_subagent(handle.id)  # stopping the turn stops its sub-agent too
            raise
        return f"[sub-agent {handle.id} finished: {handle.status}]\n{result}"

    reg.register(
        ToolSpec(
            name="spawn_agent",
            description=(
                "Hand a self-contained task to a sub-agent with its own fresh context, so the details of "
                "the work don't fill this conversation. Give it everything it needs in `task`; it can't see "
                "this conversation. Returns its final answer. With background=true it runs while you continue, "
                "and you're notified when it's done. `tools` limits its tools by glob, e.g. ['slack__*', 'recall']."
            ),
            parameters=_obj(
                {
                    "task": {"type": "string"},
                    "title": {"type": "string", "description": "A few words, shown in the activity list"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "background": {"type": "boolean"},
                    "thinking": {"type": "boolean"},
                },
                ["task"],
            ),
            handler=spawn,
            policy="allow",
            main_only=True,
        )
    )

    async def agent_status(args: dict[str, Any], ctx: ToolContext) -> str:
        rows = app.store.list_sessions(kind="subagent", limit=int(args.get("limit", 15)))
        if not rows:
            return "No sub-agents yet."
        return "\n".join(
            f"{r['id']} · {r['status']} · {r['title'] or ''}"
            + (f" · routine {r['routine']}" if r["routine"] else "")
            for r in rows
        )

    async def agent_result(args: dict[str, Any], ctx: ToolContext) -> str:
        row = app.store.get_session(args["id"])
        if not row:
            return f"No sub-agent {args['id']!r}."
        if row["status"] == "running":
            return f"Sub-agent {args['id']} is still running."
        return f"[{row['status']}] {row['title'] or ''}\n{row['result'] or '(no result)'}"

    reg.register(
        ToolSpec(
            name="agent_status",
            description="List recent sub-agents (including routine runs) and whether they're running or done.",
            parameters=_obj({"limit": {"type": "integer"}}),
            handler=agent_status,
            policy="allow",
        )
    )
    reg.register(
        ToolSpec(
            name="agent_result",
            description="Read a sub-agent's final result into this conversation.",
            parameters=_obj({"id": {"type": "string"}}, ["id"]),
            handler=agent_result,
            policy="allow",
        )
    )

    # ---- tasks & routines ------------------------------------------------------------------------------
    async def run_task(args: dict[str, Any], ctx: ToolContext) -> str:
        return await app.routines.invoke_task(args["name"], args.get("input", ""), ctx)

    async def list_procedures(args: dict[str, Any], ctx: ToolContext) -> str:
        return app.routines.describe()

    async def run_routine(args: dict[str, Any], ctx: ToolContext) -> str:
        if ctx.routine:
            return "Routines can't start other routines."
        app.routines.trigger(args["name"], trigger="agent")
        return f"Started routine {args['name']!r}; its output will appear when it finishes."

    reg.register(
        ToolSpec(
            name="list_tasks_and_routines",
            description="List the user's defined tasks (on demand) and routines (scheduled), with schedules.",
            parameters=_obj({}),
            handler=list_procedures,
            policy="allow",
        )
    )
    reg.register(
        ToolSpec(
            name="run_task",
            description="Run one of the user's defined tasks by name, with optional input text.",
            parameters=_obj({"name": {"type": "string"}, "input": {"type": "string"}}, ["name"]),
            handler=run_task,
            policy="allow",
        )
    )
    reg.register(
        ToolSpec(
            name="run_routine",
            description="Start one of the user's scheduled routines now, outside its schedule.",
            parameters=_obj({"name": {"type": "string"}}, ["name"]),
            handler=run_routine,
            policy="allow",
            main_only=True,
        )
    )

    # ---- routine state & time ---------------------------------------------------------------------------
    def _ns(ctx: ToolContext) -> str:
        return f"routine:{ctx.routine}" if ctx.routine else "agent"

    async def state_get(args: dict[str, Any], ctx: ToolContext) -> str:
        if args.get("key"):
            return json.dumps(app.store.kv_get(_ns(ctx), args["key"]), default=str)
        return json.dumps(app.store.kv_all(_ns(ctx)), indent=1, default=str)

    async def state_set(args: dict[str, Any], ctx: ToolContext) -> str:
        if args.get("delete"):
            app.store.kv_delete(_ns(ctx), args["key"])
            return f"Deleted {args['key']!r}."
        app.store.kv_set(_ns(ctx), args["key"], args.get("value"))
        return f"Saved {args['key']!r}."

    reg.register(
        ToolSpec(
            name="state_get",
            description=(
                "Read saved state that persists between runs (scoped to the current routine, if any). "
                "Omit key to get everything."
            ),
            parameters=_obj({"key": {"type": "string"}}),
            handler=state_get,
            policy="allow",
        )
    )
    reg.register(
        ToolSpec(
            name="state_set",
            description="Save state that persists between runs (scoped to the current routine), e.g. IDs already handled.",
            parameters=_obj({"key": {"type": "string"}, "value": {}, "delete": {"type": "boolean"}}, ["key"]),
            handler=state_set,
            policy="allow",
        )
    )

    async def now(args: dict[str, Any], ctx: ToolContext) -> str:
        tz = ZoneInfo(app.cfg.workday.timezone)
        return datetime.now(tz).strftime("%A %Y-%m-%d %H:%M %Z")

    reg.register(
        ToolSpec(
            name="current_time",
            description="The current date and time in the user's timezone.",
            parameters=_obj({}),
            handler=now,
            policy="allow",
        )
    )
