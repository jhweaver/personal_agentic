"""The tool-calling loop: ask the model, run the tools it asks for, repeat until it answers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from ..llm import ToolCall
from ..tools.base import ToolContext, ToolSpec, decide
from .compaction import maybe_compact
from .session import Session

if TYPE_CHECKING:
    from ..app import AgentApp

log = logging.getLogger(__name__)

STEP_LIMIT_NOTE = (
    "[system] You've hit the step limit for this task. Don't call more tools. Reply with what you "
    "found and did so far, and what's left."
)


async def run_loop(
    app: AgentApp,
    session: Session,
    *,
    tools: list[ToolSpec],
    thinking: bool,
    priority: int,
    max_steps: int,
    ctx: ToolContext,
) -> str:
    tool_map = {t.name: t for t in tools}
    schemas = [t.openai_schema() for t in tools]
    schema_chars = len(json.dumps(schemas)) if schemas else 0
    session.repair()
    try:
        return await _steps(app, session, tool_map, schemas, schema_chars, thinking, priority, max_steps, ctx)
    except asyncio.CancelledError:
        # Stopped mid-turn: give any unanswered tool calls a result so the history stays valid.
        session.repair()
        raise


async def _steps(
    app: AgentApp,
    session: Session,
    tool_map: dict[str, ToolSpec],
    schemas: list[dict[str, Any]],
    schema_chars: int,
    thinking: bool,
    priority: int,
    max_steps: int,
    ctx: ToolContext,
) -> str:
    bus = app.bus

    async def on_delta(kind: str, text: str) -> None:
        bus.emit(
            "reasoning_delta" if kind == "reasoning" else "assistant_delta",
            {"text": text},
            session=session.id,
        )

    for step in range(max_steps):
        await maybe_compact(app, session, schema_chars)
        messages = session.build()
        bus.emit(
            "status", {"state": "generating", "step": step + 1, "thinking": thinking}, session=session.id
        )
        resp = await app.llm.chat(
            messages, tools=schemas or None, thinking=thinking, on_delta=on_delta, priority=priority
        )
        session.record_usage(resp.usage, Session.message_chars(messages) + schema_chars)
        if resp.reasoning:
            bus.emit("reasoning", {"text": resp.reasoning}, session=session.id)

        msg: dict[str, Any] = {"role": "assistant", "content": resp.content or ""}
        if resp.tool_calls:
            msg["tool_calls"] = [tc.to_message() for tc in resp.tool_calls]
        session.append(msg)
        _emit_usage(app, session, resp.usage)

        if not resp.tool_calls:
            text = resp.content or "(The model returned an empty reply.)"
            bus.emit("assistant_message", {"text": text}, session=session.id)
            return text

        if resp.content:
            bus.emit("assistant_message", {"text": resp.content, "interim": True}, session=session.id)
        for tc in resp.tool_calls:
            result = await execute_tool_call(app, tc, tool_map, ctx)
            session.append({"role": "tool", "tool_call_id": tc.id, "content": result})

    # Out of steps: one last call without tools to wrap up.
    session.append({"role": "user", "content": STEP_LIMIT_NOTE})
    resp = await app.llm.chat(session.build(), thinking=False, on_delta=on_delta, priority=priority)
    text = resp.content or "(Stopped at the step limit.)"
    session.append({"role": "assistant", "content": text})
    bus.emit("assistant_message", {"text": text, "step_limit": True}, session=session.id)
    return text


def _emit_usage(app: AgentApp, session: Session, usage: dict[str, Any] | None) -> None:
    if session.kind != "main" or not usage:
        return
    app.bus.emit(
        "status",
        {
            "state": "usage",
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        },
        session=session.id,
    )


async def execute_tool_call(
    app: AgentApp, tc: ToolCall, tool_map: dict[str, ToolSpec], ctx: ToolContext
) -> str:
    bus = app.bus
    max_chars = app.cfg.context.max_tool_result_chars
    try:
        args = json.loads(tc.arguments) if tc.arguments.strip() else {}
        if not isinstance(args, dict):
            raise ValueError("arguments must be a JSON object")
    except (json.JSONDecodeError, ValueError) as e:
        bus.emit("tool_call", {"id": tc.id, "name": tc.name, "args": tc.arguments}, session=ctx.session)
        result = f"ERROR: couldn't parse the arguments as a JSON object ({e}). Try again with valid JSON."
        bus.emit(
            "tool_result", {"id": tc.id, "name": tc.name, "text": result, "ok": False}, session=ctx.session
        )
        return result

    bus.emit("tool_call", {"id": tc.id, "name": tc.name, "args": args}, session=ctx.session)
    spec = tool_map.get(tc.name)
    ok = True
    if spec is None:
        ok = False
        result = f"ERROR: there is no tool named {tc.name!r} available here."
    else:
        action = decide(spec, args, ctx)
        if action == "deny":
            ok = False
            result = "This tool call is blocked by the approval policy. Don't retry it."
        else:
            approved, note = True, ""
            if action == "ask":
                approved, note = await app.attention.request_approval(ctx, spec, args)
            if not approved:
                ok = False
                result = f"{app.cfg.agent.user_name} did not approve this call" + (
                    f": {note}" if note else "."
                )
                result += " Don't retry it unless asked."
            else:
                try:
                    out = await spec.handler(args, ctx)
                    result = out if isinstance(out, str) else json.dumps(out, indent=1, default=str)
                except Exception as e:  # tools fail; the model should hear about it
                    log.exception("tool %s failed", tc.name)
                    ok = False
                    result = f"ERROR: {type(e).__name__}: {e}"
                if note:
                    result = f"(Approved with note: {note})\n{result}"
    if len(result) > max_chars:
        result = result[:max_chars] + f"\n\n[... cut: {len(result) - max_chars} more characters]"
    bus.emit("tool_result", {"id": tc.id, "name": tc.name, "text": result, "ok": ok}, session=ctx.session)
    return result
