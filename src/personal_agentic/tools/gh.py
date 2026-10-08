"""The `gh` tool: GitHub through your own signed-in GitHub CLI, like asking Claude Code to use `gh`.

It runs `gh <args>` directly (no shell), with your existing `gh auth login`. Reading is
free: list/view/status/diff/checks, search, and `gh api` GET requests. Anything that
changes GitHub (create, comment, merge, close, edit, review, api POST/PATCH/DELETE, ...)
asks you first. Commands that touch credentials or gh's own setup are refused.

Turned on automatically when `gh` is on your PATH; set `[tools] gh = false` to turn it off.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .base import Action, ToolContext, ToolSpec

if TYPE_CHECKING:
    from ..app import AgentApp

# Never run these: credentials, gh configuration, and things that execute code locally.
BLOCKED = {
    "auth",
    "config",
    "secret",
    "variable",
    "ssh-key",
    "gpg-key",
    "extension",
    "extensions",
    "alias",
    "codespace",
    "copilot",
    "attestation",
    "completion",
    "agent-task",
}
# Subcommands that only read.
READ_VERBS = {"list", "view", "status", "diff", "checks", "search", "ls", "watch", "item-list", "field-list"}
READ_TOP = {"search", "status"}  # whole command groups that only read
_WRITE_API_FLAGS = {"-f", "-F", "--field", "--raw-field", "--input"}


class GhCommandError(ValueError):
    pass


def parse(args: str | list[str]) -> list[str]:
    argv = shlex.split(args) if isinstance(args, str) else [str(a) for a in args]
    if argv and argv[0] == "gh":
        argv = argv[1:]
    if not argv:
        raise GhCommandError("No gh command given, e.g. 'pr list --repo OWNER/REPO --json number,title'")
    if argv[0] in BLOCKED:
        raise GhCommandError(f"`gh {argv[0]}` isn't allowed from the agent")
    if argv[0] == "repo" and len(argv) > 1 and argv[1] in ("clone", "fork", "sync"):
        raise GhCommandError(f"`gh repo {argv[1]}` isn't allowed from the agent (it changes local files)")
    if argv[0] in ("pr", "issue") and len(argv) > 1 and argv[1] in ("checkout", "develop"):
        raise GhCommandError(
            f"`gh {argv[0]} {argv[1]}` isn't allowed from the agent (it changes local files)"
        )
    if argv[0] == "browse":
        raise GhCommandError("`gh browse` opens a browser; use `gh ... view --json url` to get links instead")
    return argv


def classify(argv: list[str]) -> Action:
    """'allow' for reads, 'ask' for anything that might change GitHub."""
    top = argv[0]
    if top in READ_TOP:
        return "allow"
    if top == "api":
        explicit = None
        for i, a in enumerate(argv):
            if a in ("-X", "--method") and i + 1 < len(argv):
                explicit = argv[i + 1].upper()
            elif a.startswith("--method="):
                explicit = a.split("=", 1)[1].upper()
            elif a.startswith("-X") and len(a) > 2:
                explicit = a[2:].upper()
        if len(argv) > 1 and argv[1] == "graphql":
            return "ask" if any("mutation" in a for a in argv) else "allow"
        has_body = any(a.split("=")[0] in _WRITE_API_FLAGS for a in argv)
        method = explicit or ("POST" if has_body else "GET")  # gh sends POST when fields are given
        return "allow" if method == "GET" else "ask"
    verb = argv[1] if len(argv) > 1 else ""
    return "allow" if verb in READ_VERBS else "ask"


def register_gh_tool(app: AgentApp) -> bool:
    setting = app.cfg.tools.gh
    path = shutil.which("gh")
    if setting is False or path is None:
        return False

    async def run(args: dict[str, Any], ctx: ToolContext) -> str:
        argv = parse(args["args"])
        env = {
            **os.environ,
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_PAGER": "cat",
            "NO_COLOR": "1",
            "CLICOLOR": "0",
        }
        proc = await asyncio.create_subprocess_exec(
            path,
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path.home()),  # not inside any repo: commands must name --repo
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=float(args.get("timeout") or 120))
        except TimeoutError:
            proc.kill()
            return "ERROR: gh took too long and was stopped."
        text = out.decode(errors="replace").strip()
        if proc.returncode != 0:
            msg = err.decode(errors="replace").strip()
            return f"ERROR: gh exited {proc.returncode}: {msg[:3000]}" + (f"\n{text[:3000]}" if text else "")
        return text or "(no output)"

    def policy(args: dict[str, Any], ctx: ToolContext) -> Action:
        try:
            return classify(parse(args.get("args", "")))
        except (GhCommandError, ValueError):
            return "allow"  # the handler refuses it with a clear message

    app.tools.register(
        ToolSpec(
            name="gh",
            description=(
                "Run the GitHub CLI as the user (their own `gh` login). Pass everything after `gh` in "
                "`args`, e.g. `pr list --repo OWNER/REPO --author HANDLE --state open --json "
                "number,title,url,createdAt,author`, `pr view 123 --repo OWNER/REPO --json "
                "title,body,reviews,comments`, `search prs --review-requested=@me --state open --json "
                "url,title,repository`, or `api repos/OWNER/REPO/pulls?state=open`. Always pass "
                "--repo (it doesn't run inside a repository) and prefer --json for exact fields. Reading "
                "is free; commands that change GitHub (create, comment, merge, close, review, edit, api "
                "POST/PATCH/DELETE) wait for the user's approval."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "Arguments after `gh`"},
                    "timeout": {"type": "integer", "description": "Seconds (default 120)"},
                },
                "required": ["args"],
            },
            handler=run,
            policy=policy,
        )
    )
    return True
