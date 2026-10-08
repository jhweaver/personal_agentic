"""A stand-in for LM Studio's API, scripted by the text of the last message.

"CALL name {json}" lines in a user message  -> tool calls (args streamed in pieces)
"THINK" in a user message                   -> a <think> block before the answer
"SLOW" in a user message                    -> answer slowly (for cancel tests)
a tool result as the last message           -> "Done. <tool output>"
the compaction prompt                       -> a fixed summary
anything else                               -> "Echo: <last line>"
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

MODEL = "qwen/qwen3.8-27b"
EMBED = "text-embedding-nomic-embed-text-v1.5"


def make_app() -> FastAPI:
    app = FastAPI()
    app.state.requests = []

    @app.get("/v1/models")
    async def models():
        return {"data": [{"id": MODEL, "object": "model"}, {"id": EMBED, "object": "model"}]}

    @app.get("/api/v0/models")
    async def models_v0():
        return {
            "data": [
                {
                    "id": MODEL,
                    "type": "llm",
                    "state": "loaded",
                    "max_context_length": 262144,
                    "loaded_context_length": 131072,
                },
                {"id": EMBED, "type": "embeddings", "state": "loaded", "max_context_length": 2048},
            ]
        }

    @app.post("/v1/embeddings")
    async def embeddings(req: Request):
        body = await req.json()
        data = []
        for i, text in enumerate(body["input"]):
            vec = np.zeros(64, dtype=np.float32)
            for word in re.findall(r"\w+", text.lower()):
                if word in ("search_query", "search_document"):
                    continue
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % 64] += 1
            data.append({"index": i, "embedding": vec.tolist(), "object": "embedding"})
        return {"data": data, "model": body["model"]}

    @app.post("/v1/chat/completions")
    async def chat(req: Request):
        body = await req.json()
        app.state.requests.append(body)
        if getattr(app.state, "reject_extras", False) and "chat_template_kwargs" in body:
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": "Unrecognized key: chat_template_kwargs"}, status_code=400)
        messages = body["messages"]
        last = messages[-1]
        thinking = (body.get("chat_template_kwargs") or {}).get("enable_thinking", True)
        tool_calls: list[dict] = []
        text = ""
        slow = False
        if messages[0]["role"] == "system" and "Fold the conversation" in messages[0]["content"]:
            text = "## Ongoing work\nSummarized: the user asked about several things."
        elif last["role"] == "tool":
            text = f"Done. {last['content'][:300]}"
        else:
            content = last.get("content") or ""
            calls = re.findall(r"^CALL (\S+) (\{.*\})\s*$", content, re.M)
            if calls and body.get("tools"):
                tool_calls = [{"name": n, "arguments": a} for n, a in calls]
            elif "THINK" in content and thinking:
                text = "<think>Let me ponder this carefully.</think>The answer is 42."
            else:
                slow = "SLOW" in content
                lines = [ln for ln in content.strip().splitlines() if ln.strip()]
                text = "Echo: " + (lines[-1] if lines else "")

        prompt_chars = sum(len(m.get("content") or "") for m in messages)

        async def stream():
            def chunk(delta: dict, finish: str | None = None) -> str:
                return (
                    "data: "
                    + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})
                    + "\n\n"
                )

            if text:
                pieces = [text[i : i + 7] for i in range(0, len(text), 7)]
                for p in pieces:
                    if slow:
                        await asyncio.sleep(0.2)
                    yield chunk({"content": p})
            for idx, tc in enumerate(tool_calls):
                args = tc["arguments"]
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": idx,
                                "id": f"call_{idx}",
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": ""},
                            }
                        ]
                    }
                )
                half = len(args) // 2
                yield chunk({"tool_calls": [{"index": idx, "function": {"arguments": args[:half]}}]})
                yield chunk({"tool_calls": [{"index": idx, "function": {"arguments": args[half:]}}]})
            yield chunk({}, "tool_calls" if tool_calls else "stop")
            yield (
                "data: "
                + json.dumps(
                    {"choices": [], "usage": {"prompt_tokens": prompt_chars // 4, "completion_tokens": 10}}
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app
