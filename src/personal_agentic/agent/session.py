"""A conversation the model works in: the main context, or a sub-agent's.

The message list sent to the model is:
    system prompt (static, so LM Studio can reuse its prompt cache)
    preamble (summary of compacted history + working-context snapshot), if any
    active messages (everything since the last compaction)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..app import AgentApp

PREAMBLE_ACK = "Understood. I have the earlier context and the working context."


class Session:
    def __init__(
        self,
        app: AgentApp,
        sid: str,
        kind: str,
        system_prompt: str,
        *,
        title: str | None = None,
        parent: str | None = None,
        routine: str | None = None,
        depth: int = 0,
    ):
        self.app = app
        self.id = sid
        self.kind = kind
        self.system_prompt = system_prompt
        self.title = title
        self.parent = parent
        self.routine = routine
        self.depth = depth
        store = app.store
        if store.get_session(sid) is None:
            store.upsert_session(sid, kind, title=title, parent=parent, routine=routine, status="idle")
        self.messages: list[dict[str, Any]] = store.active_messages(sid)
        self.preamble: str | None = store.get_preamble(sid)
        self.tokens_per_char: float = 1 / app.cfg.context.chars_per_token
        self.last_prompt_tokens: int | None = None

    # ---- building the request ---------------------------------------------------------
    def build(self) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        if self.preamble:
            msgs.append({"role": "user", "content": self.preamble})
            msgs.append({"role": "assistant", "content": PREAMBLE_ACK})
        msgs.extend(self.messages)
        return msgs

    def append(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg = {k: v for k, v in msg.items() if not k.startswith("_")}
        msg["_id"] = self.app.store.add_message(self.id, msg)
        self.messages.append(msg)
        return msg

    def add_note(self, text: str, ack: str = "Noted.") -> None:
        """Put information into the context without running a model turn."""
        self.repair()
        self.append({"role": "user", "content": text})
        self.append({"role": "assistant", "content": ack})

    def set_preamble(self, text: str | None) -> None:
        self.preamble = text
        if text is not None:
            self.app.store.set_preamble(self.id, text)

    def repair(self) -> None:
        """Make sure the latest tool calls all have results (a turn may have been cut off).

        Missing results are added right after the calls they belong to, which is the end
        of the list: repair runs before anything else is appended.
        """
        idx = next(
            (i for i in range(len(self.messages) - 1, -1, -1) if self.messages[i].get("tool_calls")), None
        )
        if idx is None:
            return
        answered = {m.get("tool_call_id") for m in self.messages[idx + 1 :] if m["role"] == "tool"}
        missing = [tc for tc in self.messages[idx]["tool_calls"] if tc["id"] not in answered]
        if not missing:
            return
        if any(m["role"] != "tool" for m in self.messages[idx + 1 :]):
            return  # something already followed; leave the history as it is
        for tc in missing:
            self.append({"role": "tool", "tool_call_id": tc["id"], "content": "[stopped before this ran]"})

    # ---- size -------------------------------------------------------------------------
    @staticmethod
    def message_chars(msgs: list[dict[str, Any]]) -> int:
        total = 0
        for m in msgs:
            total += len(m.get("content") or "") + 16
            if m.get("tool_calls"):
                total += len(json.dumps(m["tool_calls"]))
        return total

    def record_usage(self, usage: dict[str, Any] | None, request_chars: int) -> None:
        if not usage or not usage.get("prompt_tokens") or request_chars <= 0:
            return
        self.last_prompt_tokens = int(usage["prompt_tokens"])
        ratio = self.last_prompt_tokens / request_chars
        if 0.1 < ratio < 0.8:
            self.tokens_per_char = ratio

    def estimate_tokens(self, tool_schema_chars: int = 0) -> int:
        chars = self.message_chars(self.build()) + tool_schema_chars
        return int(chars * self.tokens_per_char)
