"""The PR watch as a Python routine: code does the filtering, the model only checks Slack.

Copy this into routines/ to use it instead of routines/pr-watch.md (disable one of them).
GitHub goes through the `gh` tool (your own GitHub CLI login); the Slack check uses the
Slack tools if you have them, or Glean search.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from personal_agentic import RoutineContext, routine

REPO = "your-org/X"
AUTHORS = ["h1", "h2", "h3"]
MIN_OPEN = timedelta(hours=3)


@routine(
    every="15m",
    window="workday",
    report="changes",
    notify=["desktop"],
    enabled=False,
    description="PRs from watched authors open 3h+ with no Slack mention go on the plate (Python version).",
)
async def pr_watch_py(ctx: RoutineContext) -> str:
    handled: list[int] = ctx.state.get("handled", [])
    added: list[str] = []
    still_open: set[int] = set()

    for author in AUTHORS:
        raw = await ctx.call_tool(
            "gh",
            {
                "args": f"pr list --repo {REPO} --state open --author {author} --json number,title,url,createdAt"
            },
        )
        try:
            items = json.loads(raw)
        except json.JSONDecodeError:
            ctx.emit(f"Couldn't read gh output for {author}: {raw[:200]}")
            continue
        for pr in items:
            number = pr["number"]
            still_open.add(number)
            created = datetime.fromisoformat(pr["createdAt"].replace("Z", "+00:00"))
            if number in handled or ctx.now() - created < MIN_OPEN:
                continue
            verdict = await ctx.ask(
                f"Search Slack for any mention of pull request {REPO}#{number} "
                f'(URL {pr["url"]}, title "{pr["title"]}"). '
                "Reply with exactly MENTIONED or NOT_MENTIONED, then one line of evidence.",
                tools=["slack__*", "glean__*"],
                thinking=False,
                title=f"slack check #{number}",
            )
            if verdict.strip().upper().startswith("NOT_MENTIONED"):
                line = f'- [ ] Review PR #{number} "{pr["title"]}" by @{author} — {pr["url"]} (pr-watch, {ctx.now():%Y-%m-%d})'
                await ctx.working_context.update("Plate", "append", line)
                added.append(line)
            handled.append(number)

    ctx.state["handled"] = [n for n in handled if n in still_open]
    return "\n".join(added) if added else "NO_UPDATE"
