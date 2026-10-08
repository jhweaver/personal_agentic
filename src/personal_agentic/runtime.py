"""Small runtime helpers shared across modules."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Coroutine
from typing import Any

# True while code runs on behalf of a turn of the main conversation (including foreground
# sub-agents it started). Anything in that state must not wait on the main queue, or it
# would wait on itself.
IN_MAIN_TURN: contextvars.ContextVar[bool] = contextvars.ContextVar("in_main_turn", default=False)


def detached_task[T](coro: Coroutine[Any, Any, T], name: str | None = None) -> asyncio.Task[T]:
    """Start a task that doesn't count as part of the current main turn."""
    ctx = contextvars.copy_context()
    ctx.run(IN_MAIN_TURN.set, False)
    return asyncio.create_task(coro, name=name, context=ctx)
