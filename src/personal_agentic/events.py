"""The shared feed. Every message, tool call, routine run and alert becomes an event.

Events are written to SQLite and pushed to every connected client (web page, terminal
chat, scripts), which is how a conversation started in the terminal shows up live on
the web page. Streaming token deltas are pushed but not stored.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .store import Store

log = logging.getLogger(__name__)

# Kinds that are pushed live but not written to the database.
EPHEMERAL = {"assistant_delta", "reasoning_delta", "status"}


class EventBus:
    def __init__(self, store: Store):
        self.store = store
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()

    def emit(
        self,
        kind: str,
        data: dict[str, Any] | None = None,
        *,
        session: str = "main",
        source: str = "agent",
    ) -> dict[str, Any]:
        data = data or {}
        ts = time.time()
        event: dict[str, Any] = {
            "id": None,
            "ts": ts,
            "session": session,
            "kind": kind,
            "source": source,
            "data": data,
        }
        if kind not in EPHEMERAL:
            event["id"] = self.store.add_event(session, kind, source, data, ts)
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # A stuck client shouldn't stall the agent; it can reload history.
                log.warning("dropping event for slow subscriber")
        return event

    def subscribe(self, maxsize: int = 5000) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)
