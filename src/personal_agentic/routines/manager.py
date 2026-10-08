"""Loads routines and tasks, runs the schedule, and routes results back to you.

Where results go:
  context: main      the prompt runs inside the one ongoing conversation; the reply is there.
  context: isolated  a sub-agent runs it; unless it reports NO_UPDATE (with report: changes),
                     its result is added to the main conversation as a note and shown on the web.
Then any `notify` channels fire, and `await_reply` opens an item that escalates if you don't answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from ..config import parse_duration
from ..notify import NotifyError, desktop_notification, send_imessage
from ..runtime import IN_MAIN_TURN, detached_task
from ..tools.base import ToolContext
from .context import RoutineContext
from .defs import NO_UPDATE, RoutineDef
from .loader import load_folder, signature

if TYPE_CHECKING:
    from ..app import AgentApp

log = logging.getLogger(__name__)

MISSED_GRACE = timedelta(minutes=15)


def _grace(d: RoutineDef) -> timedelta:
    """How late a scheduled run may start (e.g. after the Mac wakes up) before it's skipped."""
    if d.catch_up is True:
        return timedelta(days=3650)
    if d.catch_up:
        return max(MISSED_GRACE, parse_duration(d.catch_up))
    return MISSED_GRACE


class RoutineManager:
    def __init__(self, app: AgentApp):
        self.app = app
        self.defs: dict[str, RoutineDef] = {}
        self.next_runs: dict[str, datetime] = {}
        self.running: dict[str, asyncio.Task[str]] = {}
        self.errors: list[str] = []
        self._sig: tuple = ()
        self._task: asyncio.Task[None] | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.app.cfg.workday.timezone)

    def _folders(self):
        cfg = self.app.cfg
        return cfg.resolve(cfg.paths.routines_dir), cfg.resolve(cfg.paths.tasks_dir)

    # ---- loading ---------------------------------------------------------------------------------
    def load(self) -> None:
        routines_dir, tasks_dir = self._folders()
        wd = self.app.cfg.workday
        r_defs, r_err = load_folder(routines_dir, "routine", wd)
        t_defs, t_err = load_folder(tasks_dir, "task", wd)
        old = self.defs
        self.defs = {}
        for d in [*r_defs, *t_defs]:
            if d.name in self.defs:
                r_err.append(f"Duplicate name {d.name!r} ({d.source})")
                continue
            self.defs[d.name] = d
        self.errors = r_err + t_err
        for e in self.errors:
            self.app.bus.emit("error", {"text": f"Routine load error: {e}"}, source="system")
        now = datetime.now(self.tz)
        for name, d in self.defs.items():
            prev = old.get(name)
            unchanged = prev and prev.schedule and d.schedule and prev.schedule.raw == d.schedule.raw
            if d.kind == "routine" and d.enabled and d.schedule:
                if not (unchanged and name in self.next_runs):
                    nxt = d.schedule.next_after(now)
                    if nxt:
                        self.next_runs[name] = nxt
            else:
                self.next_runs.pop(name, None)
        for name in list(self.next_runs):
            if name not in self.defs:
                del self.next_runs[name]
        self._sig = signature(*self._folders())

    def describe(self) -> str:
        if not self.defs:
            return "No tasks or routines defined yet."
        lines = []
        for d in sorted(self.defs.values(), key=lambda x: (x.kind, x.name)):
            when = d.schedule.describe() if d.schedule else "on demand"
            state = "" if d.enabled else " (disabled)"
            nxt = self.next_runs.get(d.name)
            nxt_s = f", next {nxt.strftime('%a %H:%M')}" if nxt else ""
            lines.append(
                f"- {d.kind} **{d.name}**{state}: {d.description} [{when}{nxt_s}; context {d.context}]"
            )
        return "\n".join(lines)

    def listing(self) -> list[dict]:
        out = []
        for d in sorted(self.defs.values(), key=lambda x: (x.kind, x.name)):
            last = self.app.store.last_run(d.name)
            nxt = self.next_runs.get(d.name)
            out.append(
                {
                    "name": d.name,
                    "kind": d.kind,
                    "description": d.description,
                    "schedule": d.schedule.describe() if d.schedule else None,
                    "enabled": d.enabled,
                    "context": d.context,
                    "next_run": nxt.isoformat() if nxt else None,
                    "running": d.name in self.running,
                    "last_run": last,
                    "source": str(d.source) if d.source else None,
                }
            )
        return out

    # ---- schedule loop ---------------------------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for t in self.running.values():
            t.cancel()

    async def _loop(self) -> None:
        while True:
            try:
                if signature(*self._folders()) != self._sig:
                    log.info("Routine files changed; reloading")
                    self.load()
                    self.app.bus.emit("routines_reloaded", {"count": len(self.defs)}, source="system")
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            now = datetime.now(self.tz)
            soonest = min(self.next_runs.values(), default=now + timedelta(seconds=30))
            await asyncio.sleep(max(1.0, min(30.0, (soonest - now).total_seconds())))

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(self.tz)
        for name, due in list(self.next_runs.items()):
            if due > now:
                continue
            d = self.defs.get(name)
            if d is None or d.schedule is None:
                self.next_runs.pop(name, None)
                continue
            self.next_runs[name] = d.schedule.next_after(now) or now + timedelta(days=365)
            if now - due > _grace(d):
                self._skip(d, f"missed its {due.strftime('%H:%M')} slot (computer asleep or agent off?)")
                continue
            if name in self.running:
                self._skip(d, "previous run still going")
                continue
            self.trigger(name, trigger="schedule")

    def _skip(self, d: RoutineDef, why: str) -> None:
        run_id = self.app.store.start_run(d.name, "schedule", status="skipped")
        self.app.store.finish_run(run_id, "skipped", why)
        self.app.bus.emit("routine_skipped", {"routine": d.name, "reason": why}, source=f"routine:{d.name}")

    # ---- running -----------------------------------------------------------------------------------
    def trigger(
        self, name: str, trigger: str = "manual", input_text: str = "", detached: bool = True
    ) -> asyncio.Task[str]:
        d = self.defs.get(name)
        if d is None:
            raise KeyError(f"No routine or task named {name!r}")
        if name in self.running:
            return self.running[name]
        coro = self.run(d, trigger, input_text)
        task = (
            detached_task(coro, f"routine:{name}")
            if detached
            else asyncio.create_task(coro, name=f"routine:{name}")
        )
        self.running[name] = task
        task.add_done_callback(lambda _: self.running.pop(name, None))
        return task

    async def run(self, d: RoutineDef, trigger: str, input_text: str = "") -> str:
        app = self.app
        source = f"routine:{d.name}"
        run_id = app.store.start_run(d.name, trigger)
        now = datetime.now(self.tz)
        app.bus.emit(
            "routine_started", {"routine": d.name, "trigger": trigger, "run_id": run_id}, source=source
        )
        thinking = d.thinking if d.thinking is not None else app.cfg.thinking.routines
        session_id: str | None = None
        status = "done"
        try:
            if d.python_fn is not None:
                session_id = f"run-{d.name}-{uuid.uuid4().hex[:6]}"
                app.store.upsert_session(
                    session_id, "routine", title=d.name, parent="main", routine=d.name, status="running"
                )
                ctx = RoutineContext(app, d, session_id, trigger, input_text)
                result = await d.python_fn(ctx)
                output = "" if result is None else str(result)
                app.store.upsert_session(session_id, "routine", status="done", result=output)
            elif d.context == "main":
                prompt = d.render(app.cfg.agent.user_name, now, input_text)
                output = await app.submit(
                    f"[Routine: {d.name}]\n{prompt}",
                    source=source,
                    kind="routine",
                    thinking=thinking,
                    meta={"routine": d.name},
                )
                session_id = "main"
            else:
                prompt = self._isolated_prompt(d, now, input_text)
                handle = await app.spawn_subagent(
                    task=prompt,
                    title=d.name,
                    tools=d.tools,
                    thinking=thinking,
                    background=False,
                    parent="main",
                    depth=1,
                    routine=d.name,
                    max_steps=d.max_steps,
                )
                session_id = handle.id
                output = await handle.wait()
                if handle.status != "done":
                    status = "error"
        except asyncio.CancelledError:
            app.store.finish_run(run_id, "cancelled", None, session_id)
            app.bus.emit("routine_finished", {"routine": d.name, "status": "cancelled"}, source=source)
            raise
        except Exception as e:
            log.exception("routine %s failed", d.name)
            output, status = f"ERROR: {type(e).__name__}: {e}", "error"

        quiet = output.strip() in ("", NO_UPDATE)
        app.store.finish_run(run_id, status, output, session_id)
        app.bus.emit(
            "routine_finished",
            {
                "routine": d.name,
                "status": status,
                "quiet": quiet,
                "session": session_id,
                "text": output[:6000],
            },
            source=source,
        )
        if status == "error" or not quiet:
            await self._deliver(d, output, status, session_id)
        return output

    def _isolated_prompt(self, d: RoutineDef, now: datetime, input_text: str) -> str:
        user = self.app.cfg.agent.user_name
        lines = [
            f"You're running {user}'s {d.kind} \"{d.name}\" ({now.strftime('%A %Y-%m-%d %H:%M %Z')}).",
            f"Your final reply is delivered to {user}, so make it the finished result, not a description of your steps.",
        ]
        if d.report == "changes":
            lines.append(
                f"If there's nothing new worth telling {user}, reply with exactly {NO_UPDATE} and nothing else."
            )
        return "\n".join(lines) + "\n\n" + d.render(user, now, input_text)

    async def _deliver(self, d: RoutineDef, output: str, status: str, session_id: str | None) -> None:
        app = self.app
        is_main = d.context == "main" and d.python_fn is None
        if not is_main and d.report != "never":
            label = "failed" if status == "error" else "reported"
            app.submit(
                f"[Routine {d.name} {label} at {time.strftime('%H:%M')}]\n{output}",
                source=f"routine:{d.name}",
                kind="note",
            )
            if status != "error":
                await app.memory.add(
                    f"Routine {d.name} ({time.strftime('%Y-%m-%d %H:%M')}):\n{output}", kind="routine"
                )
        for channel in d.notify:
            try:
                if channel == "desktop":
                    await desktop_notification(f"Routine: {d.name}", output)
                elif channel == "imessage":
                    await send_imessage(app.cfg.notify.imessage_self or "", f"[{d.name}] {output[:1500]}")
            except NotifyError as e:
                app.bus.emit(
                    "error", {"text": f"{d.name}: couldn't notify via {channel}: {e}"}, source="system"
                )
        if d.await_reply and status != "error":
            app.attention.open_review("main", f"{d.name} is waiting for your reply", output, d.escalate)

    # ---- tasks -------------------------------------------------------------------------------------------
    async def invoke_task(self, name: str, input_text: str, ctx: ToolContext) -> str:
        d = self.defs.get(name)
        if d is None:
            return f"No task named {name!r}. Defined: {', '.join(sorted(self.defs)) or 'none'}"
        now = datetime.now(self.tz)
        if d.python_fn is None and d.context == "main":
            # Whoever asked (the main agent or a sub-agent) carries the instructions out itself.
            return f"Instructions for task {d.name!r} — carry these out now:\n\n{d.render(self.app.cfg.agent.user_name, now, input_text)}"
        # From inside a main turn the task must not wait on the main conversation (ask_main
        # raises there instead of hanging), so it keeps the caller's context.
        task = self.trigger(
            name, trigger=f"run_task:{ctx.session}", input_text=input_text, detached=not IN_MAIN_TURN.get()
        )
        return await asyncio.shield(task)
