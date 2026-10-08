"""A priority semaphore in front of LM Studio.

One local model can only do so much at once. Interactive chat (priority 0) jumps ahead
of routines (5) and background sub-agents (10). Priority applies per model call, so a
long background job yields between its steps.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

PRIORITY_INTERACTIVE = 0
PRIORITY_ROUTINE = 5
PRIORITY_BACKGROUND = 10


class LLMGate:
    def __init__(self, capacity: int = 1):
        self.capacity = max(1, capacity)
        self.active = 0
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    @property
    def queued(self) -> int:
        return sum(1 for _, _, f in self._waiters if not f.done())

    async def acquire(self, priority: int) -> None:
        if self.active < self.capacity and not self.queued:
            self.active += 1
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (priority, next(self._seq), fut))
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                # We were granted the slot just as we got cancelled: pass it on.
                self.release()
            raise

    def release(self) -> None:
        self.active -= 1
        while self._waiters:
            _, _, fut = heapq.heappop(self._waiters)
            if not fut.done():
                self.active += 1
                fut.set_result(None)
                return

    @asynccontextmanager
    async def slot(self, priority: int) -> AsyncIterator[None]:
        await self.acquire(priority)
        try:
            yield
        finally:
            self.release()
