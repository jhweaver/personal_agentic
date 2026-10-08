"""Copy MCP server settings from Claude Code into mcp.json (`pa mcp import`).

Claude Code keeps them in ~/.claude.json: user-scoped servers at the top level under
"mcpServers", and local-scoped ones (the `claude mcp add` default) under
"projects" -> "<folder>" -> "mcpServers". Project-scoped ones live in a folder's .mcp.json.
Logins don't carry over: Claude Code keeps its tokens in the macOS keychain, issued to
its own app, so each imported server that signs in needs `pa mcp login <name>` once.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ImportResult:
    added: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (name, why)
    notes: list[str] = field(default_factory=list)
    servers: dict[str, dict[str, Any]] = field(default_factory=dict)


def claude_config_path() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    if base and (Path(base).expanduser() / ".claude.json").exists():
        return Path(base).expanduser() / ".claude.json"
    return Path.home() / ".claude.json"


def collect_claude_servers(
    claude_json: Path, project_dirs: list[Path] | None = None, include_local: bool = True
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """All servers Claude Code knows about. Precedence like Claude Code: local, project, user."""
    notes: list[str] = []
    found: dict[str, tuple[int, str, dict[str, Any]]] = {}

    def offer(name: str, cfg: dict[str, Any], rank: int, where: str) -> None:
        current = found.get(name)
        if current is None or rank < current[0]:
            if current is not None and current[2] != cfg:
                notes.append(f"{name}: using the {where} definition over the {current[1]} one")
            found[name] = (rank, where, cfg)
        elif current[2] != cfg:
            notes.append(f"{name}: also defined differently in {where}; kept the {current[1]} one")

    data: dict[str, Any] = {}
    if claude_json.exists():
        data = json.loads(claude_json.read_text() or "{}")
    else:
        notes.append(f"{claude_json} not found")
    for name, cfg in (data.get("mcpServers") or {}).items():
        offer(name, cfg, 3, "user")
    if include_local:
        for folder, proj in (data.get("projects") or {}).items():
            for name, cfg in (proj.get("mcpServers") or {}).items():
                offer(name, cfg, 1, f"local ({folder})")
    for d in project_dirs or []:
        mcp_json = Path(d).expanduser() / ".mcp.json"
        if mcp_json.exists():
            for name, cfg in (json.loads(mcp_json.read_text() or "{}").get("mcpServers") or {}).items():
                offer(name, cfg, 2, f"project ({d})")
    return {name: cfg for name, (_, _, cfg) in found.items()}, notes


def _env_name(server: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", server.upper()).strip("_")


def convert(name: str, cfg: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Claude Code entry -> this agent's entry (or None and a reason to skip)."""
    kind = (cfg.get("type") or ("stdio" if "command" in cfg else "")).lower()
    if kind in ("streamable-http", "streamable_http"):
        kind = "http"
    if kind not in ("stdio", "http", "sse"):
        return None, f"type {kind or 'unknown'!r} isn't supported here"
    out: dict[str, Any] = {"type": kind}
    for key in ("url", "headers", "headersHelper", "command", "args", "env", "cwd"):
        if cfg.get(key) not in (None, "", [], {}):
            out[key] = cfg[key]
    if kind in ("http", "sse") and not out.get("url"):
        return None, "no url"
    if kind == "stdio" and not out.get("command"):
        return None, "no command"
    if isinstance(cfg.get("timeout"), int | float) and cfg["timeout"] > 0:
        out["timeout"] = max(1, round(cfg["timeout"] / 1000))  # Claude Code uses milliseconds
    oauth = cfg.get("oauth")
    if isinstance(oauth, dict) and oauth:
        o = {k: v for k, v in oauth.items() if k in ("clientId", "callbackPort", "scopes")}
        if o.get("clientId"):
            # Claude Code keeps the secret in the keychain; read ours from .env if the app needs one.
            o["clientSecret"] = f"${{{_env_name(name)}_CLIENT_SECRET}}"
        out["oauth"] = o
    return out, None


def needs_sign_in(entry: dict[str, Any]) -> bool:
    if entry.get("type") not in ("http", "sse"):
        return False
    headers = {k.lower() for k in (entry.get("headers") or {})}
    return "authorization" not in headers and not entry.get("headersHelper")


def import_servers(
    mcp_json: Path,
    claude_json: Path | None = None,
    project_dirs: list[Path] | None = None,
    include_local: bool = True,
    overwrite: bool = False,
    dry_run: bool = False,
) -> ImportResult:
    res = ImportResult()
    found, res.notes = collect_claude_servers(
        claude_json or claude_config_path(), project_dirs, include_local
    )
    existing: dict[str, Any] = {}
    if mcp_json.exists():
        existing = json.loads(mcp_json.read_text() or "{}")
    servers: dict[str, Any] = dict(existing.get("mcpServers") or {})
    for name, cfg in sorted(found.items()):
        entry, why = convert(name, cfg)
        if entry is None:
            res.skipped.append((name, why or "unsupported"))
            continue
        if name in servers and not overwrite:
            res.kept.append(name)
            continue
        (res.replaced if name in servers else res.added).append(name)
        servers[name] = entry
    res.servers = servers
    if not dry_run and (res.added or res.replaced):
        existing["mcpServers"] = servers
        mcp_json.write_text(json.dumps(existing, indent=2) + "\n")
    return res
