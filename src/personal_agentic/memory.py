"""Long-term memory: notes the agent saves, plus archived conversation from compaction.

Stored in SQLite with nomic embeddings and searched by cosine similarity in numpy, which
is plenty fast for one person's history. If the embedding model isn't loaded, saving
still works (embeddings get filled in later) and search falls back to keywords.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

from .llm.embeddings import Embedder, EmbeddingError, from_blob, to_blob
from .store import Store

log = logging.getLogger(__name__)


def chunk_text(text: str, size: int = 1800, overlap: int = 200) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    chunks, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            end = cut if cut > 0 else end
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in chunks if c]


class Memory:
    def __init__(self, store: Store, embedder: Embedder):
        self.store = store
        self.embedder = embedder

    async def add(self, text: str, kind: str = "note", space: str = "text", meta: dict | None = None) -> int:
        blob = None
        try:
            vec = await self.embedder.embed([text], space=space, purpose="document")  # type: ignore[arg-type]
            blob = to_blob(vec[0])
        except EmbeddingError as e:
            log.warning("Saving memory without an embedding for now: %s", e)
        return self.store.add_memory(kind, text, blob, space=space, meta=meta)

    async def add_many(self, texts: list[str], kind: str, meta: dict | None = None) -> int:
        if not texts:
            return 0
        try:
            vecs = await self.embedder.embed(texts, purpose="document")
            blobs: list[bytes | None] = [to_blob(v) for v in vecs]
        except EmbeddingError as e:
            log.warning("Archiving %d chunks without embeddings for now: %s", len(texts), e)
            blobs = [None] * len(texts)
        for t, b in zip(texts, blobs, strict=True):
            self.store.add_memory(kind, t, b, meta=meta)
        return len(texts)

    async def backfill(self) -> int:
        """Embed memories saved while the embedding model was unavailable."""
        rows = self.store.memories_missing_embeddings()
        if not rows:
            return 0
        by_space: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            by_space.setdefault(r["space"], []).append(r)
        done = 0
        for space, items in by_space.items():
            try:
                vecs = await self.embedder.embed([r["text"] for r in items], space=space, purpose="document")  # type: ignore[arg-type]
            except EmbeddingError:
                continue
            for r, v in zip(items, vecs, strict=True):
                self.store.set_memory_embedding(r["id"], to_blob(v))
                done += 1
        return done

    async def search(
        self, query: str, k: int = 8, kinds: list[str] | None = None, space: str = "text"
    ) -> list[dict[str, Any]]:
        rows = [r for r in self.store.memories(space=space, kinds=kinds) if r["embedding"]]
        try:
            if not rows:
                raise EmbeddingError("nothing embedded yet")
            q = (await self.embedder.embed([query], space=space, purpose="query"))[0]  # type: ignore[arg-type]
        except EmbeddingError:
            hits = self.store.search_memories_text(query, limit=k)
            return [{**h, "score": None} for h in hits if not kinds or h["kind"] in kinds]
        dim = q.shape[0]
        rows = [r for r in rows if len(r["embedding"]) == dim * 4]
        if not rows:
            return []
        mat = np.stack([from_blob(r["embedding"]) for r in rows])
        scores = mat @ q
        order = np.argsort(-scores)[:k]
        return [{**rows[i], "score": float(scores[i])} for i in order]


def format_hits(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "No matching memories."
    lines = []
    for h in hits:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(h["ts"]))
        score = f" score={h['score']:.2f}" if h.get("score") is not None else ""
        lines.append(f"### #{h['id']} {h['kind']} · {when}{score}\n{h['text']}")
    return "\n\n".join(lines)
