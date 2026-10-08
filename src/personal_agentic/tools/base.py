"""Tool registry and approval policy.

Every tool the model can call (built-in, MCP, or your own Python in tools/) is a ToolSpec
in one registry. Before a call runs, the policy decides: allow, ask (you approve on the
web page or in the terminal), or deny.
"""

from __future__ import annotations

import fnmatch
import inspect
import re
import types
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from ..app import AgentApp

Action = Literal["allow", "ask", "deny"]
Handler = Callable[[dict[str, Any], "ToolContext"], Awaitable[Any]]
PolicyFn = Callable[[dict[str, Any], "ToolContext"], Action | None]

_NAME_OK = re.compile(r"[^a-zA-Z0-9_-]")

# Words in a tool name that suggest it changes something outside the agent.
WRITE_WORDS = {
    "send",
    "post",
    "create",
    "update",
    "delete",
    "remove",
    "reply",
    "write",
    "edit",
    "upload",
    "share",
    "archive",
    "move",
    "add",
    "invite",
    "react",
    "set",
    "publish",
    "merge",
    "close",
    "comment",
    "modify",
    "trash",
    "label",
    "approve",
    "submit",
    "assign",
    "rename",
    "insert",
    "append",
    "patch",
    "put",
    "schedule",
    "cancel",
    "forward",
    "kick",
    "join",
    "leave",
    "pin",
}


def safe_tool_name(name: str) -> str:
    """OpenAI-style function names: [a-zA-Z0-9_-]{1,64}."""
    return _NAME_OK.sub("_", name)[:64]


def name_words(name: str) -> set[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", spaced) if w}


@dataclass
class ToolContext:
    app: AgentApp
    session: str
    source: str = "agent"
    routine: str | None = None
    depth: int = 0


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    origin: str = "builtin"  # builtin | mcp:<server> | custom:<file>
    read_only: bool | None = None
    destructive: bool | None = None
    policy: PolicyFn | Action | None = None  # tool's own default, before config rules
    main_only: bool = False  # e.g. spawn_agent: not offered to sub-agents
    extra: dict[str, Any] = field(default_factory=dict)

    def openai_schema(self) -> dict[str, Any]:
        params = self.parameters or {"type": "object", "properties": {}}
        if "type" not in params:
            params = {"type": "object", **params}
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description[:4000], "parameters": params},
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self.version = 0  # bumps whenever the set of tools changes

    def register(self, spec: ToolSpec) -> None:
        spec.name = safe_tool_name(spec.name)
        self._tools[spec.name] = spec
        self.version += 1

    def unregister_origin(self, origin: str) -> None:
        names = [n for n, s in self._tools.items() if s.origin == origin]
        for n in names:
            del self._tools[n]
        if names:
            self.version += 1

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda s: s.name)

    def select(self, patterns: list[str] | None = None, *, depth: int = 0) -> list[ToolSpec]:
        """Tools matching any of the glob patterns ("*" or None means all). '!glob' excludes."""
        include = [p for p in (patterns or ["*"]) if not p.startswith("!")] or ["*"]
        exclude = [p[1:] for p in (patterns or []) if p.startswith("!")]
        out = []
        for spec in self.all():
            if depth > 0 and spec.main_only:
                continue
            if any(fnmatch.fnmatch(spec.name, p) for p in include) and not any(
                fnmatch.fnmatch(spec.name, p) for p in exclude
            ):
                out.append(spec)
        return out


def decide(spec: ToolSpec, args: dict[str, Any], ctx: ToolContext) -> Action:
    """Config rules win, then the tool's own policy, then the MCP default."""
    tools_cfg = ctx.app.cfg.tools
    for rule in tools_cfg.rules:
        if fnmatch.fnmatch(spec.name, rule.match):
            return rule.action
    if spec.policy is not None:
        if callable(spec.policy):
            verdict = spec.policy(args, ctx)
            if verdict:
                return verdict
        else:
            return spec.policy
    if spec.origin.startswith("mcp:"):
        if tools_cfg.mcp_default != "auto":
            return tools_cfg.mcp_default
        if spec.read_only is True:
            return "allow"
        if spec.destructive is True:
            return "ask"
        bare = spec.extra.get("mcp_name", spec.name)
        return "ask" if name_words(bare) & WRITE_WORDS else "allow"
    return "allow"


# ---- @tool decorator for your own Python tools ---------------------------------------

_PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object", list: "array"}


def _json_type(annotation: Any) -> dict[str, Any]:
    if annotation is inspect.Parameter.empty or annotation is Any:
        return {}
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        return _json_type(non_none[0]) if len(non_none) == 1 else {}
    if origin is Literal:
        return {"enum": list(args)}
    if origin in (list, tuple, set):
        schema: dict[str, Any] = {"type": "array"}
        if args:
            schema["items"] = _json_type(args[0])
        return schema
    if origin is dict:
        return {"type": "object"}
    if annotation in _PY_TO_JSON:
        return {"type": _PY_TO_JSON[annotation]}
    return {}


def schema_from_function(fn: Callable[..., Any]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    props: dict[str, Any] = {}
    required: list[str] = []
    param_docs = _param_docs(fn.__doc__ or "")
    for pname, param in sig.parameters.items():
        if pname in ("ctx", "self") or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        prop = _json_type(hints.get(pname, param.annotation))
        if pname in param_docs:
            prop["description"] = param_docs[pname]
        props[pname] = prop
        if param.default is inspect.Parameter.empty:
            required.append(pname)
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def _param_docs(doc: str) -> dict[str, str]:
    """Pick up 'name: description' lines from an Args: section."""
    out: dict[str, str] = {}
    in_args = False
    for line in doc.splitlines():
        s = line.strip()
        if s.lower() in ("args:", "arguments:", "parameters:"):
            in_args = True
            continue
        if in_args:
            m = re.match(r"^(\w+)\s*(?:\([^)]*\))?:\s*(.+)$", s)
            if m:
                out[m.group(1)] = m.group(2)
            elif not s:
                in_args = False
    return out


def tool(
    name: str | None = None,
    *,
    description: str | None = None,
    policy: Action | None = None,
    read_only: bool | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a function in tools/*.py as a tool the agent can call.

        @tool(policy="allow")
        async def lookup_employee(name: str, ctx=None) -> str:
            '''Find someone in the company directory.

            Args:
                name: Full or partial name.
            '''

    Sync or async; a parameter named `ctx` receives the ToolContext.
    """

    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        doc = inspect.getdoc(fn) or ""
        summary = doc.split("\n\nArgs:")[0].split("\nArgs:")[0].strip()
        fn.__pa_tool__ = {  # type: ignore[attr-defined]
            "name": name or fn.__name__,
            "description": description or summary or fn.__name__,
            "policy": policy,
            "read_only": read_only,
        }
        return fn

    return wrap


def spec_from_function(fn: Callable[..., Any], origin: str) -> ToolSpec:
    meta = fn.__pa_tool__  # type: ignore[attr-defined]
    wants_ctx = "ctx" in inspect.signature(fn).parameters

    async def handler(args: dict[str, Any], ctx: ToolContext) -> Any:
        kwargs = dict(args)
        if wants_ctx:
            kwargs["ctx"] = ctx
        result = fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    return ToolSpec(
        name=meta["name"],
        description=meta["description"],
        parameters=schema_from_function(fn),
        handler=handler,
        origin=origin,
        read_only=meta["read_only"],
        policy=meta["policy"],
    )
