from __future__ import annotations

import asyncio
import json
import os
import stat

import pytest

from personal_agentic.tools.gh import GhCommandError, classify, parse, register_gh_tool

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
if args[:2] == ["pr", "list"]:
    print(json.dumps([{"number": 7, "title": "Fix retries", "url": "https://github.com/o/r/pull/7",
                       "createdAt": "2026-10-08T10:00:00Z", "cwd": os.getcwd(),
                       "prompt_disabled": os.environ.get("GH_PROMPT_DISABLED")}]))
elif args[:1] == ["fail"]:
    print("boom", file=sys.stderr); sys.exit(4)
else:
    print("ran: " + " ".join(args))
"""


@pytest.fixture
def fake_gh(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    gh = bindir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return gh


@pytest.mark.parametrize(
    "args,expected",
    [
        ("pr list --repo o/r --json number", "allow"),
        ("pr view 12 --repo o/r --comments", "allow"),
        ("search prs --review-requested=@me", "allow"),
        ("api repos/o/r/pulls --paginate", "allow"),
        ("api -X GET repos/o/r/pulls -f state=open", "allow"),
        ("api graphql -f query='query { viewer { login } }'", "allow"),
        ("run view 99 --repo o/r --log-failed", "allow"),
        ("pr create --repo o/r --title x --body y", "ask"),
        ("pr comment 3 --repo o/r --body hi", "ask"),
        ("pr merge 3 --repo o/r", "ask"),
        ("api repos/o/r/issues -f title=x", "ask"),
        ("api --method=DELETE repos/o/r/labels/x", "ask"),
        ("api graphql -f query='mutation { addStar(input:{}) { clientMutationId } }'", "ask"),
        ("workflow run ci.yml --repo o/r", "ask"),
    ],
)
def test_classify(args, expected):
    assert classify(parse(args)) == expected


@pytest.mark.parametrize(
    "args", ["auth token", "secret list", "repo clone o/r", "pr checkout 3", "extension install x", "", "gh"]
)
def test_blocked(args):
    with pytest.raises(GhCommandError):
        parse(args)


async def test_gh_tool_reads_freely_and_asks_before_writing(agent, fake_gh):
    assert register_gh_tool(agent)
    reply = await agent.submit(
        'CALL gh {"args": "pr list --repo o/r --state open --author h1 --json number,title"}', source="web"
    )
    data = json.loads(reply.removeprefix("Done. "))
    assert data[0]["number"] == 7
    assert data[0]["cwd"] == os.path.expanduser("~") and data[0]["prompt_disabled"] == "1"
    assert not agent.store.open_attention()

    fut = agent.submit('CALL gh {"args": "pr comment 7 --repo o/r --body looks-good"}', source="web")
    for _ in range(100):
        items = agent.store.open_attention(kind="approval")
        if items:
            break
        await asyncio.sleep(0.02)
    assert items and items[0]["title"].startswith("Approve gh")
    agent.attention.resolve(items[0]["id"], True)
    assert "ran: pr comment 7 --repo o/r --body looks-good" in await asyncio.wait_for(fut, 20)

    reply = await asyncio.wait_for(agent.submit('CALL gh {"args": "auth token"}', source="web"), 20)
    assert "isn't allowed" in reply
    reply = await asyncio.wait_for(agent.submit('CALL gh {"args": "fail list"}', source="web"), 20)
    assert "gh exited 4: boom" in reply


async def test_gh_tool_off_or_missing(agent, monkeypatch, tmp_path):
    agent.tools.unregister_origin("builtin")
    monkeypatch.setenv("PATH", str(tmp_path))  # no gh here
    assert register_gh_tool(agent) is False
    agent.cfg.tools.gh = False
    assert register_gh_tool(agent) is False
