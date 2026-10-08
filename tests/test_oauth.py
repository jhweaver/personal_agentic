from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from pathlib import Path

import httpx
import pytest

from personal_agentic.mcp_import import convert, import_servers, needs_sign_in
from personal_agentic.mcp_servers import sign_in
from personal_agentic.oauth import FileTokenStorage, token_path, uses_oauth


async def fake_browser(url: str) -> None:
    """What the user's browser does: open the sign-in page, which redirects back to us."""
    async with httpx.AsyncClient() as http:
        r = await http.get(url, follow_redirects=False)
        assert r.status_code == 302, r.text
        back = await http.get(r.headers["location"])
        assert "Signed in" in back.text


def server_cfg(oauth_server, port, **oauth):
    return {"type": "http", "url": oauth_server["url"], "oauth": {"callbackPort": port, **oauth}}


async def test_sign_in_with_dynamic_registration(cfg, oauth_server, free_port):
    sc = server_cfg(oauth_server, free_port)
    count = await asyncio.wait_for(sign_in("docs", sc, cfg.data_dir, fake_browser), 30)
    assert count == 2
    path = token_path(cfg.data_dir, "docs")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved["tokens"]["access_token"] and saved["tokens"]["refresh_token"]
    assert saved["client_info"]["client_id"].startswith("dyn-")
    q = oauth_server["state"].authorize_requests[-1]
    assert q["redirect_uri"] == f"http://localhost:{free_port}/callback"


async def test_agent_uses_saved_sign_in_and_reports_needs_login(agent, cfg, oauth_server, free_port):
    sc = server_cfg(oauth_server, free_port)
    Path(cfg.paths.mcp_config).write_text(json.dumps({"mcpServers": {"docs": sc}}))
    await agent.mcp.start(wait=20)
    st = agent.mcp.status()[0]
    assert st["status"] == "needs_login" and st["auth"] == "oauth" and st["signed_in"] is False
    assert "pa mcp login docs" in st["error"]
    assert any(
        e["kind"] == "mcp_status" and e["data"]["status"] == "needs_login" for e in agent.store.events()
    )

    # Sign in the way the web page does: the agent hands back a URL for the browser.
    res = await agent.mcp.start_login("docs")
    assert res["url"].startswith(oauth_server["url"].replace("/mcp", "/authorize"))
    await fake_browser(res["url"])
    for _ in range(200):
        if agent.mcp.status()[0]["status"] == "connected":
            break
        await asyncio.sleep(0.05)
    st = agent.mcp.status()[0]
    assert st["status"] == "connected" and st["tools"] == 2 and st["signed_in"] is True
    reply = await agent.submit('CALL docs__search_docs {"query": "onboarding"}', source="web")
    assert "3 docs about onboarding" in reply
    await agent.mcp.stop()


async def test_expired_token_is_refreshed_without_a_new_sign_in(agent, cfg, oauth_server, free_port):
    oauth_server["state"].expires_in = 1
    sc = server_cfg(oauth_server, free_port)
    await asyncio.wait_for(sign_in("docs", sc, cfg.data_dir, fake_browser), 30)
    await asyncio.sleep(1.2)  # access token now expired
    Path(cfg.paths.mcp_config).write_text(json.dumps({"mcpServers": {"docs": sc}}))
    await agent.mcp.start(wait=20)
    assert agent.mcp.status()[0]["status"] == "connected", agent.mcp.status()
    assert oauth_server["state"].refresh_count >= 1
    await agent.mcp.stop()


async def test_pre_registered_client_when_server_has_no_registration(cfg, oauth_server, free_port):
    from fake_oauth_server import STATIC_ID, STATIC_SECRET

    oauth_server["state"].dcr = False
    sc = server_cfg(
        oauth_server, free_port, clientId=STATIC_ID, clientSecret=STATIC_SECRET, scopes="read write"
    )
    count = await asyncio.wait_for(sign_in("slackish", sc, cfg.data_dir, fake_browser), 30)
    assert count == 2
    q = oauth_server["state"].authorize_requests[-1]
    assert q["client_id"] == STATIC_ID and q["scope"] == "read write"
    token_req = oauth_server["state"].token_requests[-1]
    assert token_req["client_secret"] == STATIC_SECRET
    saved = json.loads(token_path(cfg.data_dir, "slackish").read_text())
    assert "client_info" not in saved  # the secret never gets written to the token file


async def test_sign_in_errors_are_clear(cfg, oauth_server, free_port):
    with pytest.raises(ValueError, match="stdio"):
        await sign_in("x", {"command": "echo"}, cfg.data_dir, fake_browser)
    with pytest.raises(ValueError, match="Authorization header"):
        await sign_in(
            "x",
            {"type": "http", "url": oauth_server["url"], "headers": {"Authorization": "Bearer t"}},
            cfg.data_dir,
            fake_browser,
        )

    async def never(url):  # user never finishes signing in
        return None

    with pytest.raises(Exception, match="No sign-in completed"):
        await sign_in("x", server_cfg(oauth_server, free_port), cfg.data_dir, never, timeout=0.5)


def test_uses_oauth_rules():
    assert uses_oauth({"type": "http", "url": "https://x"})
    assert not uses_oauth({"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer t"}})
    assert not uses_oauth({"command": "npx"})
    assert not uses_oauth({"type": "http", "url": "https://x", "oauth": False})


def test_token_storage_summary_and_clear(tmp_path):
    st = FileTokenStorage(tmp_path / "oauth" / "s.json")
    assert st.summary()["signed_in"] is False
    from mcp.shared.auth import OAuthToken

    asyncio.run(st.set_tokens(OAuthToken(access_token="a", refresh_token="r", expires_in=100)))
    s = st.summary()
    assert s["signed_in"] and s["can_refresh"] and s["expires_at"] > time.time()
    asyncio.run(st.set_tokens(OAuthToken(access_token="b", expires_in=100)))  # refresh token not rotated
    assert json.loads((tmp_path / "oauth" / "s.json").read_text())["tokens"]["refresh_token"] == "r"
    assert st.clear() and not st.clear()


def test_import_from_claude_code(tmp_path):
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "atlassian": {"type": "http", "url": "https://mcp.atlassian.com/v1/mcp"},
                    "glean": {
                        "type": "http",
                        "url": "https://acme-be.glean.com/mcp/default",
                        "timeout": 90000,
                    },
                    "ws-thing": {"type": "ws", "url": "wss://x"},
                },
                "projects": {
                    "/Users/harper/work": {
                        "mcpServers": {
                            "slack": {
                                "type": "http",
                                "url": "https://mcp.slack.com/mcp",
                                "oauth": {"clientId": "123.456", "callbackPort": 8080},
                            },
                            "github": {
                                "command": "github-mcp-server",
                                "args": ["stdio"],
                                "env": {"GITHUB_TOKEN": "x"},
                            },
                            "atlassian": {"type": "sse", "url": "https://mcp.atlassian.com/v1/sse"},
                        }
                    }
                },
            }
        )
    )
    proj = tmp_path / "repo"
    proj.mkdir()
    (proj / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "wiki": {
                        "type": "http",
                        "url": "https://wiki/mcp",
                        "headers": {"Authorization": "Bearer ${WIKI}"},
                    }
                }
            }
        )
    )
    mcp_json = tmp_path / "mcp.json"
    mcp_json.write_text(json.dumps({"mcpServers": {"github": {"command": "mine"}}}))

    res = import_servers(mcp_json, claude_json=claude_json, project_dirs=[proj])
    assert sorted(res.added) == ["atlassian", "glean", "slack", "wiki"]
    assert res.kept == ["github"]
    assert res.skipped == [("ws-thing", "type 'ws' isn't supported here")]
    assert any("atlassian" in n for n in res.notes)  # local definition wins over user, like Claude Code
    out = json.loads(mcp_json.read_text())["mcpServers"]
    assert out["github"] == {"command": "mine"}
    assert out["atlassian"]["type"] == "sse"
    assert out["glean"]["timeout"] == 90
    assert out["slack"]["oauth"] == {
        "clientId": "123.456",
        "callbackPort": 8080,
        "clientSecret": "${SLACK_CLIENT_SECRET}",
    }
    assert [n for n in res.added if needs_sign_in(out[n])] == ["atlassian", "glean", "slack"]

    dry = import_servers(tmp_path / "other.json", claude_json=claude_json, include_local=False, dry_run=True)
    assert sorted(dry.added) == ["atlassian", "glean"] and not (tmp_path / "other.json").exists()
    assert convert("x", {"type": "http"}) == (None, "no url")


def test_token_path_stays_inside_the_oauth_folder(tmp_path):
    assert token_path(tmp_path, "../../etc/x").parent == tmp_path / "oauth"
    assert token_path(tmp_path, "atlassian").name == "atlassian.json"
