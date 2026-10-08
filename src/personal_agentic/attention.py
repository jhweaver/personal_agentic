"""Things waiting on you: tool approvals and routine results that want a reply.

Each item shows on the web page first. If nobody responds, it escalates on a schedule
(by default a desktop alert after 20 minutes, then an iMessage to you after 90),
only during the workday unless configured otherwise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .config import EscalationStep, parse_duration
from .notify import NotifyError, desktop_notification, send_imessage

if TYPE_CHECKING:
    from .app import AgentApp
    from .tools.base import ToolContext, ToolSpec

log = logging.getLogger(__name__)


def in_workday(app: AgentApp, when: datetime | None = None) -> bool:
    wd = app.cfg.workday
    tz = ZoneInfo(wd.timezone)
    now = (when or datetime.now(tz)).astimezone(tz)
    if now.strftime("%a").lower()[:3] not in [d.lower()[:3] for d in wd.days]:
        return False
    if now.date() in wd.holidays:
        return False
    return wd.start <= now.strftime("%H:%M") < wd.end


class AttentionManager:
    def __init__(self, app: AgentApp):
        self.app = app
        self._waiters: dict[int, asyncio.Future[tuple[bool, str]]] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._escalation_loop(), name="escalation")
        # Approvals can't survive a restart (the call that wanted them is gone).
        for item in self.app.store.open_attention(kind="approval"):
            self.app.store.resolve_attention(item["id"], "expired", "agent restarted")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for fut in self._waiters.values():
            if not fut.done():
                fut.set_result((False, "agent shutting down"))

    def _plan(self, steps: list[EscalationStep] | None) -> list[dict[str, Any]]:
        steps = self.app.cfg.notify.default_escalation if steps is None else steps
        return [s.model_dump() for s in steps]

    # ---- approvals ----------------------------------------------------------------------
    async def request_approval(
        self, ctx: ToolContext, spec: ToolSpec, args: dict[str, Any]
    ) -> tuple[bool, str]:
        where = f" (routine {ctx.routine})" if ctx.routine else ""
        title = f"Approve {spec.name}{where}?"
        body = json.dumps(args, indent=1, default=str)
        aid = self.app.store.add_attention(
            ctx.session, "approval", title, body, self._plan(None), data={"tool": spec.name, "args": args}
        )
        fut: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
        self._waiters[aid] = fut
        self.app.bus.emit(
            "approval_request",
            {"attention_id": aid, "tool": spec.name, "args": args, "title": title, "routine": ctx.routine},
            session=ctx.session,
        )
        timeout = parse_duration(self.app.cfg.tools.approval_timeout).total_seconds()
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except asyncio.CancelledError:
            # The turn that wanted this was stopped: take it off your list.
            self.app.store.resolve_attention(aid, "expired", "cancelled")
            self.app.bus.emit(
                "attention_resolved", {"attention_id": aid, "status": "expired"}, session=ctx.session
            )
            raise
        except TimeoutError:
            self.app.store.resolve_attention(aid, "expired", "no response")
            self.app.bus.emit(
                "attention_resolved", {"attention_id": aid, "status": "expired"}, session=ctx.session
            )
            return False, "no response before the approval timed out"
        finally:
            self._waiters.pop(aid, None)

    def resolve(self, aid: int, approved: bool, note: str = "", via: str = "web") -> bool:
        item = self.app.store.get_attention(aid)
        if not item or item["status"] != "open":
            return False
        if item["kind"] == "approval":
            status = "approved" if approved else "denied"
        else:
            status = "resolved"
        self.app.store.resolve_attention(aid, status, note or None)
        fut = self._waiters.get(aid)
        if fut and not fut.done():
            fut.set_result((approved, note))
        self.app.bus.emit(
            "attention_resolved",
            {"attention_id": aid, "status": status, "note": note, "via": via},
            session=item["session"],
        )
        return True

    # ---- reviews (a routine result that wants your reply) ---------------------------------
    def open_review(
        self, session: str, title: str, body: str, escalation: list[EscalationStep] | None = None
    ) -> int:
        aid = self.app.store.add_attention(session, "review", title, body, self._plan(escalation))
        self.app.bus.emit(
            "review_request", {"attention_id": aid, "title": title, "body": body[:2000]}, session=session
        )
        return aid

    def on_user_message(self, session: str) -> None:
        """Replying in a conversation answers any reviews waiting there."""
        for item in self.app.store.open_attention(session=session, kind="review"):
            self.resolve(item["id"], True, "replied", via="chat")

    # ---- escalation ---------------------------------------------------------------------------
    async def _escalation_loop(self) -> None:
        while True:
            try:
                await self.escalate_due()
            except Exception:
                log.exception("escalation check failed")
            await asyncio.sleep(20)

    async def escalate_due(self, now: float | None = None) -> None:
        now = now or time.time()
        cfg = self.app.cfg.notify
        for item in self.app.store.open_attention():
            steps = item["escalation"]
            done = item["steps_done"]
            if done >= len(steps):
                continue
            step = steps[done]
            if now - item["ts"] < parse_duration(step["after"]).total_seconds():
                continue
            if not cfg.escalate_outside_workday and not in_workday(self.app):
                continue
            await self._send(item, step["via"])
            self.app.store.set_attention_steps(item["id"], done + 1)

    async def _send(self, item: dict[str, Any], via: str) -> None:
        user = self.app.cfg.agent.user_name
        verb = "needs your approval" if item["kind"] == "approval" else "is waiting for your reply"
        text = f"{item['title']} — {verb}. Open {self.app.cfg.server_url}"
        try:
            if via == "desktop":
                await desktop_notification("Agent: waiting on you", item["title"], subtitle=verb)
            elif via == "imessage":
                await send_imessage(self.app.cfg.notify.imessage_self or "", f"[agent] {text}")
            self.app.bus.emit(
                "escalated",
                {"attention_id": item["id"], "via": via, "title": item["title"]},
                session=item["session"],
            )
        except NotifyError as e:
            log.warning("Couldn't escalate to %s via %s: %s", user, via, e)
            self.app.bus.emit(
                "error",
                {"text": f"Escalation via {via} failed: {e}"},
                session=item["session"],
                source="system",
            )
