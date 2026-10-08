"""Embeddings from LM Studio's /v1/embeddings, with nomic's task prefixes.

nomic-embed-text expects "search_query: " on queries and "search_document: " on stored
text; nomic-embed-code expects an instruction on queries only. The prefixes live in config.
"""

from __future__ import annotations

import logging
from typing import Literal

import httpx
import numpy as np

from ..config import EmbeddingModelConfig, EmbeddingsConfig

log = logging.getLogger(__name__)

Space = Literal["text", "code"]
Purpose = Literal["query", "document"]


class EmbeddingError(RuntimeError):
    pass


class Embedder:
    def __init__(self, cfg: EmbeddingsConfig, default_base_url: str, api_key: str = "lm-studio"):
        self.cfg = cfg
        self.default_base_url = default_base_url.rstrip("/")
        self._http = httpx.AsyncClient(timeout=120.0, headers={"Authorization": f"Bearer {api_key}"})

    async def aclose(self) -> None:
        await self._http.aclose()

    def _model(self, space: Space) -> EmbeddingModelConfig:
        m = self.cfg.code if space == "code" else self.cfg.text
        if m is None:
            raise EmbeddingError(f"No embedding model configured for {space!r}")
        return m

    def available_spaces(self) -> list[str]:
        return ["text"] + (["code"] if self.cfg.code else [])

    async def embed(
        self, texts: list[str], space: Space = "text", purpose: Purpose = "document"
    ) -> np.ndarray:
        """Returns an (n, dim) float32 array of unit vectors."""
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        m = self._model(space)
        prefix = m.query_prefix if purpose == "query" else m.document_prefix
        inputs = [prefix + t[: m.max_chars] for t in texts]
        base = (m.base_url or self.default_base_url).rstrip("/")
        vectors: list[list[float]] = []
        for i in range(0, len(inputs), 32):
            batch = inputs[i : i + 32]
            try:
                r = await self._http.post(f"{base}/embeddings", json={"model": m.model, "input": batch})
            except httpx.HTTPError as e:
                raise EmbeddingError(f"Embedding request failed: {e}") from e
            if r.status_code != 200:
                raise EmbeddingError(f"Embedding model {m.model!r} returned {r.status_code}: {r.text[:300]}")
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
            vectors.extend(d["embedding"] for d in data)
        arr = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
