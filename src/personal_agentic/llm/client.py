"""Chat client for LM Studio's OpenAI-compatible API (/v1/chat/completions).

Handles streaming, tool calls, and the model's reasoning, which can arrive three ways:
a separate `reasoning_content` / `reasoning` field (LM Studio's "separate reasoning"
setting), an inline <think>...</think> block, or a bare "...</think>" when the chat
template opens the block itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import LLMConfig
from .gate import PRIORITY_INTERACTIVE, LLMGate

log = logging.getLogger(__name__)

DeltaCallback = Callable[[str, str], Awaitable[None] | None]  # (kind: "content"|"reasoning", text)


class LLMError(RuntimeError):
    pass


class BadRequest(LLMError):
    pass


# Fields every OpenAI-compatible server understands; used if LM Studio rejects the extras.
PLAIN_KEYS = {"model", "messages", "stream", "max_tokens", "temperature", "top_p", "tools", "tool_choice"}
EXTRA_FIELD_HINTS = (
    "chat_template_kwargs",
    "reasoning_effort",
    "top_k",
    "min_p",
    "presence_penalty",
    "stream_options",
    "unrecognized",
    "unknown field",
    "unknown parameter",
    "extra field",
    "extra_forbidden",
    "unexpected",
)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text from the model

    def to_message(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class LLMResponse:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


class ThinkSplitter:
    """Routes streamed text inside a leading <think>...</think> block to 'reasoning'."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self) -> None:
        self.state = "start"  # start | think | content
        self.buf = ""

    def feed(self, text: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self.buf += text
        while self.buf:
            if self.state == "start":
                stripped = self.buf.lstrip()
                if stripped.startswith(self.OPEN):
                    self.buf = stripped[len(self.OPEN) :]
                    self.state = "think"
                elif self.OPEN.startswith(stripped):
                    return out  # might still become "<think>"; wait for more
                else:
                    self.state = "content"
            elif self.state == "think":
                idx = self.buf.find(self.CLOSE)
                if idx >= 0:
                    if idx:
                        out.append(("reasoning", self.buf[:idx]))
                    self.buf = self.buf[idx + len(self.CLOSE) :].lstrip("\n")
                    self.state = "content"
                else:
                    # Hold back a tail that could be the start of "</think>".
                    keep = 0
                    for k in range(1, len(self.CLOSE)):
                        if self.buf.endswith(self.CLOSE[:k]):
                            keep = k
                    emit = self.buf[: len(self.buf) - keep]
                    if emit:
                        out.append(("reasoning", emit))
                    self.buf = self.buf[len(self.buf) - keep :]
                    return out
            else:
                out.append(("content", self.buf))
                self.buf = ""
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self.buf:
            return []
        kind = "reasoning" if self.state == "think" else "content"
        text, self.buf = self.buf, ""
        return [(kind, text)]


def split_reasoning(content: str) -> tuple[str, str]:
    """Final pass: pull any reasoning still embedded in the content."""
    reasoning = ""
    text = content
    stripped = text.lstrip()
    if stripped.startswith("<think>"):
        text = stripped[len("<think>") :]
    if "</think>" in text:
        before, _, after = text.rpartition("</think>")
        reasoning = before.replace("<think>", "").strip()
        text = after
    return text.strip(), reasoning


class LLMClient:
    def __init__(self, cfg: LLMConfig, gate: LLMGate | None = None):
        self.cfg = cfg
        self.gate = gate or LLMGate(cfg.max_parallel)
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.timeout_s, connect=10.0),
            headers={"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {},
        )
        self._context_window: int | None = None
        self._plain_only = False

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def root_url(self) -> str:
        url = self.cfg.base_url.rstrip("/")
        return url[: -len("/v1")] if url.endswith("/v1") else url

    # ---- model info ---------------------------------------------------------------
    async def list_models(self) -> list[dict[str, Any]]:
        r = await self._http.get(f"{self.cfg.base_url.rstrip('/')}/models")
        r.raise_for_status()
        return r.json().get("data", [])

    async def context_window(self, refresh: bool = False) -> int:
        """The loaded model's context length (config override, else ask LM Studio)."""
        if self.cfg.context_window:
            return self.cfg.context_window
        if self._context_window and not refresh:
            return self._context_window
        found = await self._detect_context_window()
        if found:
            self._context_window = found
            return found
        # Don't remember the guess: LM Studio may just not be up yet.
        log.warning("Couldn't read the model's context length from LM Studio; assuming 32768 for now")
        return 32768

    async def _detect_context_window(self) -> int | None:
        model = self.cfg.model
        for url in (f"{self.root_url}/api/v1/models", f"{self.root_url}/api/v0/models"):
            try:
                r = await self._http.get(url, timeout=5.0)
                if r.status_code != 200:
                    continue
                body = r.json()
            except (httpx.HTTPError, ValueError):
                continue
            entries = body.get("data") or body.get("models") or (body if isinstance(body, list) else [])
            for entry in entries:
                ids = {str(entry.get(k, "")) for k in ("id", "key", "model_key", "modelKey", "path")}
                if model not in ids and not any(i.endswith(model) for i in ids if i):
                    continue
                n = _find_context_length(entry)
                if n:
                    return n
        return None

    # ---- chat -------------------------------------------------------------------------
    def _body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        thinking: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        cfg = self.cfg
        body: dict[str, Any] = {
            "model": cfg.model,
            "messages": [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages],
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_tokens or cfg.max_output_tokens,
        }
        body.update(cfg.sampling_thinking if thinking else cfg.sampling_fast)
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if cfg.thinking_control in ("chat_template_kwargs", "both"):
            body["chat_template_kwargs"] = {"enable_thinking": thinking}
        if cfg.thinking_control in ("reasoning_effort", "both"):
            if thinking and cfg.reasoning_effort:
                body["reasoning_effort"] = cfg.reasoning_effort
            elif not thinking:
                body["reasoning_effort"] = "none"
        return body

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        thinking: bool = False,
        on_delta: DeltaCallback | None = None,
        priority: int = PRIORITY_INTERACTIVE,
        max_tokens: int | None = None,
        retries: int = 2,
    ) -> LLMResponse:
        body = self._body(messages, tools, thinking, max_tokens)
        if self._plain_only:
            body = {k: v for k, v in body.items() if k in PLAIN_KEYS}
        attempt = 0
        while True:
            try:
                async with self.gate.slot(priority):
                    return await self._stream(body, on_delta)
            except BadRequest as e:
                about_fields = any(w in str(e).lower() for w in EXTRA_FIELD_HINTS)
                if self._plain_only or set(body) <= PLAIN_KEYS or not about_fields:
                    raise LLMError(str(e)) from e
                log.warning(
                    "LM Studio rejected extra request fields (%s); retrying with standard ones only", e
                )
                self._plain_only = True
                body = {k: v for k, v in body.items() if k in PLAIN_KEYS}
            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as e:
                attempt += 1
                if attempt > retries:
                    raise LLMError(f"Can't reach LM Studio at {self.cfg.base_url}: {e}") from e
                await asyncio.sleep(2 * attempt)

    async def _stream(self, body: dict[str, Any], on_delta: DeltaCallback | None) -> LLMResponse:
        url = f"{self.cfg.base_url.rstrip('/')}/chat/completions"
        resp = LLMResponse()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        splitter = ThinkSplitter()

        async def emit(kind: str, text: str) -> None:
            if kind == "reasoning":
                reasoning_parts.append(text)
            else:
                content_parts.append(text)
            if on_delta and text:
                r = on_delta(kind, text)
                if asyncio.iscoroutine(r):
                    await r

        async with self._http.stream("POST", url, json=body) as r:
            if r.status_code != 200:
                text = (await r.aread()).decode(errors="replace")
                if r.status_code in (400, 422):
                    raise BadRequest(f"LM Studio returned {r.status_code}: {text[:1000]}")
                raise LLMError(f"LM Studio returned {r.status_code}: {text[:1000]}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    raise LLMError(f"LM Studio error: {chunk['error']}")
                if chunk.get("usage"):
                    resp.usage = chunk["usage"]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                    if rc:
                        await emit("reasoning", rc)
                    if delta.get("content"):
                        for kind, text in splitter.feed(delta["content"]):
                            await emit(kind, text)
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", len(calls))
                        slot = calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        resp.finish_reason = choice["finish_reason"]
        for kind, text in splitter.flush():
            await emit(kind, text)

        content, inline_reasoning = split_reasoning("".join(content_parts))
        resp.content = content
        resp.reasoning = (
            "".join(reasoning_parts) + ("\n" + inline_reasoning if inline_reasoning else "")
        ).strip()
        resp.tool_calls = [
            ToolCall(
                id=c["id"] or f"call_{uuid.uuid4().hex[:12]}",
                name=c["name"],
                arguments=c["arguments"] or "{}",
            )
            for _, c in sorted(calls.items())
            if c["name"]
        ]
        return resp


def _find_context_length(entry: Any) -> int | None:
    """Look through an LM Studio model entry for the loaded context length."""
    preferred = ("loaded_context_length", "loadedContextLength")
    fallback = ("context_length", "contextLength", "max_context_length", "maxContextLength")

    def walk(obj: Any, keys: tuple[str, ...]) -> int | None:
        if isinstance(obj, dict):
            for k in keys:
                v = obj.get(k)
                if isinstance(v, int) and v > 0:
                    return v
            for v in obj.values():
                found = walk(v, keys)
                if found:
                    return found
        elif isinstance(obj, list):
            for v in obj:
                found = walk(v, keys)
                if found:
                    return found
        return None

    if isinstance(entry, dict):
        for inst_key in ("loaded_instances", "loadedInstances"):
            if entry.get(inst_key):
                found = walk(entry[inst_key], preferred + fallback)
                if found:
                    return found
    return walk(entry, preferred) or walk(entry, fallback)
