"""Configuration: one TOML file, with defaults for everything.

Lookup order for the config file:
  1. $PA_CONFIG
  2. ./config.toml (the repo you run from)
  3. ~/.personal_agentic/config.toml

Relative paths in the config resolve against the directory the config file lives in.
"""

from __future__ import annotations

import os
import re
import tomllib
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

DEFAULT_DATA_DIR = Path("~/.personal_agentic").expanduser()

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|hr|d)?\s*$", re.I)


def parse_duration(value: str | int | float | timedelta) -> timedelta:
    """'90s', '15m', '2h', '1d', or a number of seconds."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, int | float):
        return timedelta(seconds=value)
    m = _DURATION_RE.match(str(value))
    if not m:
        raise ValueError(f"Can't parse duration {value!r}; use e.g. '30s', '15m', '2h', '1d'")
    n = float(m.group(1))
    unit = (m.group(2) or "s").lower()
    mult = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600, "d": 86400}[unit]
    return timedelta(seconds=n * mult)


class LLMConfig(BaseModel):
    base_url: str = "http://localhost:1234/v1"
    model: str = "qwen/qwen3.8-27b"
    api_key: str = "lm-studio"  # LM Studio ignores it when auth is off
    # None = ask LM Studio what the loaded model's context length is.
    context_window: int | None = None
    max_output_tokens: int = 16384
    timeout_s: float = 900
    # How to switch the model's thinking on/off per request.
    #  chat_template_kwargs: sends {"chat_template_kwargs": {"enable_thinking": bool}}
    #  reasoning_effort:     sends {"reasoning_effort": ...} when thinking, "none" otherwise
    #  both / none
    thinking_control: Literal["chat_template_kwargs", "reasoning_effort", "both", "none"] = (
        "chat_template_kwargs"
    )
    reasoning_effort: str | None = None  # e.g. "medium"; None = model default
    # Sampling presets. Extra keys (top_k, min_p, ...) are passed through to LM Studio.
    sampling_thinking: dict[str, Any] = Field(
        default_factory=lambda: {
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
        }
    )
    sampling_fast: dict[str, Any] = Field(
        default_factory=lambda: {
            "temperature": 0.7,
            "top_p": 0.8,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 1.5,
        }
    )
    # Max simultaneous requests to LM Studio. Interactive chat jumps the queue.
    max_parallel: int = 1


class EmbeddingModelConfig(BaseModel):
    model: str
    base_url: str | None = None  # defaults to llm.base_url
    query_prefix: str = ""
    document_prefix: str = ""
    max_chars: int = 6000


class EmbeddingsConfig(BaseModel):
    text: EmbeddingModelConfig = Field(
        default_factory=lambda: EmbeddingModelConfig(
            model="text-embedding-nomic-embed-text-v1.5",
            query_prefix="search_query: ",
            document_prefix="search_document: ",
        )
    )
    code: EmbeddingModelConfig | None = Field(
        default_factory=lambda: EmbeddingModelConfig(
            model="nomic-embed-code",
            query_prefix="Represent this query for searching relevant code: ",
            document_prefix="",
        )
    )


class ContextConfig(BaseModel):
    # Compact the main conversation when it passes this many tokens. Long prompts are
    # slow to re-process on a Mac, so this sits well below the model's maximum.
    compact_at_tokens: int = 100_000
    # How much recent conversation to keep verbatim after compacting.
    keep_recent_tokens: int = 24_000
    # Tool results longer than this are cut (the full text stays in the event log).
    max_tool_result_chars: int = 40_000
    chars_per_token: float = 3.6


class ThinkingConfig(BaseModel):
    chat: bool = False
    routines: bool = True
    subagents: bool = True
    compaction: bool = False


class AgentConfig(BaseModel):
    user_name: str = "Harper"
    max_steps: int = 40
    subagent_max_steps: int = 30
    max_subagent_depth: int = 1


class WorkdayConfig(BaseModel):
    timezone: str = "America/Chicago"
    days: list[str] = Field(default_factory=lambda: ["mon", "tue", "wed", "thu", "fri"])
    start: str = "08:00"
    end: str = "17:30"
    holidays: list[date] = Field(default_factory=list)


class EscalationStep(BaseModel):
    after: str  # duration, e.g. "30m"
    via: Literal["desktop", "imessage", "web"]

    @field_validator("after")
    @classmethod
    def _check(cls, v: str) -> str:
        parse_duration(v)
        return v


class NotifyConfig(BaseModel):
    desktop: bool = True
    # Your own phone number or Apple ID email, for iMessages to yourself.
    imessage_self: str | None = None
    default_escalation: list[EscalationStep] = Field(
        default_factory=lambda: [
            EscalationStep(after="20m", via="desktop"),
            EscalationStep(after="90m", via="imessage"),
        ]
    )
    escalate_outside_workday: bool = False


class PolicyRule(BaseModel):
    match: str  # glob on the tool name, e.g. "slack__*" or "*__send_*"
    action: Literal["allow", "ask", "deny"]


class ToolsConfig(BaseModel):
    # "auto" uses MCP tool annotations and the tool name (send/post/delete/... => ask).
    mcp_default: Literal["auto", "allow", "ask"] = "auto"
    rules: list[PolicyRule] = Field(default_factory=list)
    approval_timeout: str = "4h"


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class PathsConfig(BaseModel):
    data_dir: str = str(DEFAULT_DATA_DIR)
    routines_dir: str = "routines"
    tasks_dir: str = "tasks"
    tools_dir: str = "tools"
    mcp_config: str = "mcp.json"
    system_prompt: str = "prompts/system.md"
    working_context: str = "~/.personal_agentic/working_context.md"


class Config(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    thinking: ThinkingConfig = Field(default_factory=ThinkingConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    workday: WorkdayConfig = Field(default_factory=WorkdayConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)

    # Set by load_config: the directory relative paths resolve against.
    base_dir: Path = Field(default_factory=Path.cwd, exclude=True)
    config_file: Path | None = Field(default=None, exclude=True)

    def resolve(self, p: str) -> Path:
        path = Path(os.path.expandvars(p)).expanduser()
        return path if path.is_absolute() else (self.base_dir / path)

    @property
    def data_dir(self) -> Path:
        return self.resolve(self.paths.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "agent.db"

    @property
    def server_url(self) -> str:
        return f"http://{self.server.host}:{self.server.port}"


def find_config_file() -> Path | None:
    env = os.environ.get("PA_CONFIG")
    if env:
        return Path(env).expanduser()
    for candidate in (Path.cwd() / "config.toml", DEFAULT_DATA_DIR / "config.toml"):
        if candidate.exists():
            return candidate
    return None


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path).expanduser() if path else find_config_file()
    data: dict[str, Any] = {}
    base_dir = Path.cwd()
    if path and path.exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)
        base_dir = path.parent.resolve()
    cfg = Config.model_validate(data)
    cfg.base_dir = base_dir
    cfg.config_file = path if path and path.exists() else None
    _load_dotenv(base_dir)
    return cfg


def _load_dotenv(base_dir: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    for p in (base_dir / ".env", DEFAULT_DATA_DIR / ".env"):
        if p.exists():
            load_dotenv(p, override=False)
