"""Keeping one long-running context: summarize old turns, archive them to memory.

When the conversation passes `context.compact_at_tokens`, the oldest messages are
summarized into the preamble, written to long-term memory (searchable with `recall`),
and dropped from the active context. Recent turns stay word for word.
"""

from __future__ import annotations

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from ..llm import PRIORITY_INTERACTIVE
from ..memory import chunk_text
from .session import Session

if TYPE_CHECKING:
    from ..app import AgentApp

log = logging.getLogger(__name__)

SUMMARY_SYSTEM = """You maintain the long-term memory of {user}'s personal work agent.
Fold the conversation below into the running summary so the agent can carry on without
the original messages. Keep: decisions, commitments and deadlines, open loops and
follow-ups, facts about people, projects and preferences, what routines found, and
results of tool calls that still matter (with names, links and IDs). Drop chit-chat,
raw data dumps and anything superseded. Write in compact Markdown under these headings:
## Ongoing work
## Decisions and facts
## Open loops
## Recent activity (dated)
Stay under about 1500 words."""


def render_transcript(msgs: list[dict[str, Any]], tool_chars: int = 1500) -> str:
    lines = []
    for m in msgs:
        role = m["role"]
        content = (m.get("content") or "").strip()
        if role == "tool":
            if len(content) > tool_chars:
                content = content[:tool_chars] + " [...]"
            lines.append(f"[tool result] {content}")
            continue
        if content:
            lines.append(f"[{role}] {content}")
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            lines.append(f"[assistant called {fn.get('name')}] {fn.get('arguments', '')[:500]}")
    return "\n\n".join(lines)


def _cut_index(session: Session, keep_tokens: int) -> int:
    """Index of the first message to keep: a user turn, about keep_tokens from the end."""
    msgs = session.messages
    budget = keep_tokens / max(session.tokens_per_char, 1e-6)
    total = 0
    idx = len(msgs)
    for i in range(len(msgs) - 1, -1, -1):
        total += Session.message_chars([msgs[i]])
        if total > budget:
            break
        idx = i
    # Move forward to a user message so we never split a tool call from its result.
    while idx < len(msgs) and msgs[idx]["role"] != "user":
        idx += 1
    if idx >= len(msgs):
        # The last turn alone is bigger than the budget: keep from the last user message.
        idx = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i]["role"] == "user"), 0)
    return idx


async def maybe_compact(app: AgentApp, session: Session, schema_chars: int = 0, force: bool = False) -> bool:
    cfg = app.cfg.context
    window = await app.llm.context_window()
    threshold = min(cfg.compact_at_tokens, int(window * 0.75))
    tokens = session.estimate_tokens(schema_chars)
    if not force and tokens < threshold:
        return False
    cut = _cut_index(session, min(cfg.keep_recent_tokens, threshold // 3))
    if cut <= 0:
        log.warning("Context is %d tokens but there's nothing old enough to compact", tokens)
        return False
    old, keep = session.messages[:cut], session.messages[cut:]
    app.bus.emit("status", {"state": "compacting", "tokens": tokens}, session=session.id)

    transcript = render_transcript(old)
    summary = await summarize(app, session.preamble or "", transcript, window)

    if session.kind == "main":
        await app.memory.add_many(
            chunk_text(transcript), kind="archive", meta={"session": session.id, "archived_at": time.time()}
        )
    upto = old[-1]["_id"]
    app.store.archive_messages(session.id, upto)
    session.messages = keep
    session.set_preamble(app.build_preamble(session, summary))
    after = session.estimate_tokens(schema_chars)
    app.bus.emit(
        "compaction",
        {"before_tokens": tokens, "after_tokens": after, "archived_messages": len(old)},
        session=session.id,
        source="system",
    )
    return True


async def summarize(app: AgentApp, previous: str, transcript: str, window: int) -> str:
    """Rolling summary; splits the transcript if it won't fit in one request."""
    max_chars = int(window * app.cfg.context.chars_per_token * 0.5)
    pieces = [transcript[i : i + max_chars] for i in range(0, len(transcript), max_chars)] or [""]
    summary = _strip_preamble(previous)
    system = SUMMARY_SYSTEM.format(user=app.cfg.agent.user_name)
    for piece in pieces:
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"Running summary so far:\n{summary or '(none yet)'}\n\nConversation to fold in:\n{piece}",
            },
        ]
        resp = await app.llm.chat(
            messages, thinking=app.cfg.thinking.compaction, priority=PRIORITY_INTERACTIVE, max_tokens=6000
        )
        summary = resp.content.strip() or summary
    return summary


def _strip_preamble(preamble: str) -> str:
    """Pull the summary section back out of a previous preamble."""
    if not preamble:
        return ""
    start = preamble.find("## Summary of earlier conversation")
    end = preamble.find("## Working context")
    if start < 0:
        return preamble
    body = preamble[start + len("## Summary of earlier conversation") : end if end > start else None]
    return body.strip()


def debug_dump(session: Session) -> str:
    return json.dumps(session.build(), indent=1)[:20000]
