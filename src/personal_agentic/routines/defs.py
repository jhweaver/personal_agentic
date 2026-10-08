"""Routine and task definitions, from Markdown files or Python functions.

A routine runs on a schedule; a task runs when asked (`/task name ...`, `pa task`, or
the agent's run_task tool). Both share one format:

    ---
    description: One line about what it does
    schedule: {at: "16:00", days: workdays}   # routines only
    context: main          # main = in the one ongoing conversation; isolated = a sub-agent
    thinking: true         # optional; defaults from [thinking] in config
    tools: ["*"]           # isolated only: globs of tools it may use
    report: always         # isolated only: always | changes (stay quiet on NO_UPDATE) | never
    notify: [desktop]      # also alert here when it finishes (web is automatic)
    await_reply: true      # you're expected to answer; escalates if you don't
    escalate: [{after: 30m, via: desktop}, {after: 2h, via: imessage}]
    vars: {repo: "org/project"}   # usable in the prompt as {{repo}}
    enabled: true
    ---
    The prompt. {{user_name}}, {{today}}, {{now}}, {{weekday}} and {{input}} are filled in.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from ..config import EscalationStep
from .schedule import Schedule

NO_UPDATE = "NO_UPDATE"

PythonFn = Callable[..., Awaitable[Any]]


@dataclass
class RoutineDef:
    name: str
    kind: Literal["routine", "task"]
    prompt: str = ""
    description: str = ""
    schedule: Schedule | None = None
    context: Literal["main", "isolated"] = "isolated"
    thinking: bool | None = None
    tools: list[str] = field(default_factory=lambda: ["*"])
    report: Literal["always", "changes", "never"] = "always"
    notify: list[str] = field(default_factory=list)
    await_reply: bool = False
    escalate: list[EscalationStep] | None = None
    vars: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    catch_up: bool | str = False  # true, or how late a missed run may still start ("2h")
    max_steps: int | None = None
    source: Path | None = None
    python_fn: PythonFn | None = None

    def render(self, user_name: str, now: datetime, input_text: str = "") -> str:
        values = {
            "user_name": user_name,
            "today": now.strftime("%Y-%m-%d"),
            "now": now.strftime("%Y-%m-%d %H:%M %Z"),
            "weekday": now.strftime("%A"),
            "input": input_text,
            **{k: (", ".join(map(str, v)) if isinstance(v, list) else v) for k, v in self.vars.items()},
        }
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(values.get(m.group(1), m.group(0))), self.prompt)


def _decorator(kind: Literal["routine", "task"]):
    def outer(
        name: str | None = None,
        *,
        schedule: Any = None,
        cron: str | None = None,
        at: str | None = None,
        days: Any = None,
        every: str | None = None,
        window: Any = None,
        description: str = "",
        context: Literal["main", "isolated"] = "isolated",
        thinking: bool | None = None,
        report: Literal["always", "changes", "never"] = "always",
        notify: list[str] | None = None,
        await_reply: bool = False,
        escalate: list[dict[str, str]] | None = None,
        vars: dict[str, Any] | None = None,
        enabled: bool = True,
        catch_up: bool | str = False,
    ) -> Callable[[PythonFn], PythonFn]:
        if schedule is None:
            if cron:
                schedule = {"cron": cron}
            elif at:
                schedule = {"at": at, "days": days or "workdays"}
            elif every:
                schedule = {"every": every, "window": window}

        def wrap(fn: PythonFn) -> PythonFn:
            fn.__pa_routine__ = {  # type: ignore[attr-defined]
                "kind": kind,
                "name": name or fn.__name__.replace("_", "-"),
                "schedule": schedule,
                "description": description or (fn.__doc__ or "").strip().split("\n")[0],
                "context": context,
                "thinking": thinking,
                "report": report,
                "notify": notify or [],
                "await_reply": await_reply,
                "escalate": escalate,
                "vars": vars or {},
                "enabled": enabled,
                "catch_up": catch_up,
            }
            return fn

        return wrap

    return outer


routine = _decorator("routine")
task = _decorator("task")
