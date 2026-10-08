from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from personal_agentic.agent.compaction import maybe_compact
from personal_agentic.tools.base import ToolContext, ToolSpec, decide

HERE = Path(__file__).resolve().parent


def kinds(agent, session="main"):
    return [e["kind"] for e in agent.store.events(session=session, limit=2000)]


async def test_chat_turn_and_history(agent):
    reply = await agent.submit("hello agent", source="terminal")
    assert reply == "Echo: hello agent"
    k = kinds(agent)
    assert k.index("user_message") < k.index("assistant_message")
    user_ev = next(e for e in agent.store.events() if e["kind"] == "user_message")
    assert user_ev["source"] == "terminal"
    # the model sees a timestamp header with the source
    first_user = agent.main.messages[0]["content"]
    assert "via terminal]" in first_user.splitlines()[0]
    # the conversation persists: a fresh Session object loads it back
    from personal_agentic.agent.session import Session

    again = Session(agent, "main", "main", "sys")
    assert [m["role"] for m in again.messages] == ["user", "assistant"]


async def test_tool_call_updates_working_context(agent):
    args = json.dumps({"section": "Plate", "action": "append", "text": "- [ ] Call Dana (test, 2026-10-08)"})
    reply = await agent.submit(f"CALL working_context_update {args}", source="web")
    assert reply.startswith("Done.")
    assert "Call Dana" in agent.wc.read()
    k = kinds(agent)
    assert "tool_call" in k and "tool_result" in k


async def test_external_wc_change_is_shown_as_diff(agent):
    await agent.submit("first", source="web")
    agent.wc.update("Notes", "append", "- the launch moved to Friday", author="user:web")
    await agent.submit("second", source="web")
    last_user = [m for m in agent.main.messages if m["role"] == "user"][-1]["content"]
    assert "working context changed" in last_user and "+- the launch moved to Friday" in last_user
    await agent.submit("third", source="web")
    last_user = [m for m in agent.main.messages if m["role"] == "user"][-1]["content"]
    assert "working context changed" not in last_user


async def test_approval_flow(agent):
    calls = []

    async def handler(args, ctx):
        calls.append(args)
        return "posted"

    agent.tools.register(
        ToolSpec(
            name="slack__post_message",
            description="post",
            parameters={"type": "object"},
            handler=handler,
            origin="mcp:slack",
        )
    )
    task = agent.submit('CALL slack__post_message {"channel": "#team", "text": "hi"}', source="web")
    for _ in range(100):
        items = agent.store.open_attention(kind="approval")
        if items:
            break
        await asyncio.sleep(0.02)
    assert items and items[0]["title"].startswith("Approve slack__post_message")
    agent.attention.resolve(items[0]["id"], False, "not now")
    reply = await task
    assert calls == []
    assert "did not approve" in agent.main.messages[-2]["content"]
    assert reply.startswith("Done.")

    task = agent.submit('CALL slack__post_message {"channel": "#team", "text": "hi"}', source="web")
    for _ in range(100):
        items = agent.store.open_attention(kind="approval")
        if items:
            break
        await asyncio.sleep(0.02)
    agent.attention.resolve(items[0]["id"], True)
    await task
    assert calls == [{"channel": "#team", "text": "hi"}]


async def test_policy_defaults(agent):
    ctx = ToolContext(agent, "main")

    def spec(name, **kw):
        return ToolSpec(
            name=name,
            description="",
            parameters={},
            handler=None,
            origin="mcp:x",
            extra={"mcp_name": name.split("__")[-1]},
            **kw,
        )

    assert decide(spec("x__search_messages"), {}, ctx) == "allow"
    assert decide(spec("x__send_email"), {}, ctx) == "ask"
    assert decide(spec("x__createDraft"), {}, ctx) == "ask"
    assert decide(spec("x__do_thing", read_only=True), {}, ctx) == "allow"
    assert decide(spec("x__list_things", destructive=True), {}, ctx) == "ask"
    imsg = agent.tools.get("send_imessage")
    assert decide(imsg, {"to": "me"}, ctx) == "allow"
    assert decide(imsg, {"to": "(555) 555-0100"}, ctx) == "allow"
    assert decide(imsg, {"to": "+15555550199"}, ctx) == "ask"
    from personal_agentic.config import PolicyRule

    agent.cfg.tools.rules = [PolicyRule(match="x__send_*", action="deny")]
    assert decide(spec("x__send_email"), {}, ctx) == "deny"
    agent.cfg.tools.rules = []


async def test_subagent_foreground_and_background(agent):
    args = json.dumps({"task": "Summarize the plate", "title": "plate check"})
    reply = await agent.submit(f"CALL spawn_agent {args}", source="web")
    assert "sub-agent" in reply and "finished: done" in reply
    subs = agent.store.list_sessions(kind="subagent")
    assert subs and subs[0]["status"] == "done" and subs[0]["result"].startswith("Echo:")
    # the sub-agent can't spawn more sub-agents
    sub_tools = {t.name for t in agent.tools.select(["*"], depth=1)}
    assert "spawn_agent" not in sub_tools and "recall" in sub_tools

    args = json.dumps({"task": "Look into the thing", "title": "bg", "background": True})
    await agent.submit(f"CALL spawn_agent {args}", source="web")
    for _ in range(200):
        notes = [
            e
            for e in agent.store.events()
            if e["kind"] == "note" and "Background sub-agent" in e["data"]["text"]
        ]
        if notes and not agent.main_busy and agent.queue.empty():
            break
        await asyncio.sleep(0.02)
    assert notes
    assert any("Background sub-agent" in (m.get("content") or "") for m in agent.main.messages)


async def test_routines_isolated_quiet_and_main(agent, cfg):
    rdir = Path(cfg.paths.routines_dir)
    (rdir / "quiet.md").write_text(
        '---\ndescription: q\nschedule: {every: 15m}\ncontext: isolated\nreport: changes\n---\nCALL state_set {"key": "n", "value": 1}\n'
    )
    (rdir / "loud.md").write_text(
        "---\ndescription: l\ncontext: isolated\nnotify: [desktop]\n---\nSay hello to {{user_name}}\n"
    )
    (rdir / "inmain.md").write_text(
        "---\ndescription: m\ncontext: main\nawait_reply: true\n---\nPlan {{weekday}}\n"
    )
    agent.routines.load()
    assert set(agent.routines.defs) == {"quiet", "loud", "inmain"}
    assert "quiet" in agent.routines.next_runs

    out = await agent.routines.trigger("quiet")
    assert out.startswith("Done.")  # fake model doesn't say NO_UPDATE, so it reports
    assert agent.store.kv_get("routine:quiet", "n") == 1

    out = await agent.routines.trigger("loud")
    assert out == "Echo: Say hello to Harper"
    await asyncio.sleep(0.1)
    assert any("Routine loud reported" in (m.get("content") or "") for m in agent.main.messages)
    assert any(r["kind"] == "routine" for r in agent.store.memories())

    out = await agent.routines.trigger("inmain")
    assert out.startswith("Echo: Plan")
    reviews = agent.store.open_attention(kind="review")
    assert reviews and "inmain" in reviews[0]["title"]
    await agent.submit("looks good", source="web")
    assert not agent.store.open_attention(kind="review")


async def test_routine_no_update_is_quiet(agent, cfg):
    (Path(cfg.paths.routines_dir) / "nu.md").write_text(
        "---\ncontext: isolated\nreport: changes\n---\nNO_UPDATE\n"
    )
    agent.routines.load()
    # fake echoes the last line: "Echo: NO_UPDATE" isn't quiet, so patch the def's prompt to make it reply exactly
    from personal_agentic.routines.defs import NO_UPDATE

    async def fn(ctx):
        return NO_UPDATE

    agent.routines.defs["nu"].python_fn = fn
    before = len(agent.main.messages)
    await agent.routines.trigger("nu")
    await asyncio.sleep(0.05)
    assert len(agent.main.messages) == before
    fin = [e for e in agent.store.events() if e["kind"] == "routine_finished"][-1]
    assert fin["data"]["quiet"] is True


async def test_python_routine_and_custom_tool(agent, cfg):
    (Path(cfg.paths.tools_dir) / "mytools.py").write_text(
        "from personal_agentic import tool\n\n@tool(policy='allow')\ndef add(a: int, b: int) -> str:\n    '''Add.'''\n    return str(a + b)\n"
    )
    (Path(cfg.paths.routines_dir) / "py_r.py").write_text(
        "from personal_agentic import routine\n\n"
        "@routine(every='1h', description='py test')\n"
        "async def py_check(ctx):\n"
        "    total = await ctx.call_tool('add', {'a': 2, 'b': 3})\n"
        "    ctx.state['total'] = total\n"
        "    answer = await ctx.ask('what is up', tools=['current_time'])\n"
        "    await ctx.working_context.update('Notes', 'append', f'- total {total}')\n"
        "    return f'{total} / {answer}'\n"
    )
    from personal_agentic.tools.custom import load_custom_tools

    assert load_custom_tools(agent) == ["add"]
    agent.routines.load()
    out = await agent.routines.trigger("py-check")
    assert out == "5 / Echo: what is up"
    assert agent.store.kv_get("routine:py-check", "total") == "5"
    assert "- total 5" in agent.wc.read()


async def test_slash_commands(agent):
    r = await agent.submit("/think", source="web")
    assert "on" in r and agent.main_thinking
    r = await agent.submit("/fast", source="web")
    assert not agent.main_thinking
    r = await agent.submit("/think THINK hard", source="web")
    assert r == "The answer is 42."
    assert any(e["kind"] == "reasoning" for e in agent.store.events())
    r = await agent.submit("/status", source="web")
    assert "context" in r
    r = await agent.submit("/run nope", source="web")
    assert "No task or routine" in r


async def test_compaction_archives_and_summarizes(agent):
    for i in range(6):
        await agent.submit(f"message number {i} " + "lorem ipsum " * 200, source="web")
    agent.cfg.context.compact_at_tokens = 1000
    agent.cfg.context.keep_recent_tokens = 500
    n_before = len(agent.main.messages)
    done = await maybe_compact(agent, agent.main)
    assert done
    assert len(agent.main.messages) < n_before
    assert agent.main.messages[0]["role"] == "user"
    assert "Summarized" in agent.main.preamble and "## Working context" in agent.main.preamble
    archived = agent.store.memories(kinds=["archive"])
    assert archived and archived[0]["embedding"] is not None
    hits = await agent.memory.search("message number 0 lorem", k=3, kinds=["archive"])
    assert hits
    # and the agent keeps working afterwards
    assert (await agent.submit("after compaction", source="web")) == "Echo: after compaction"


async def test_stop_cancels_turn(agent):
    task = agent.submit("SLOW please take your time", source="web")
    await asyncio.sleep(0.4)
    assert agent.stop_current()
    assert await task == "(stopped)"
    assert (await agent.submit("next", source="web")) == "Echo: next"


async def test_escalation(agent, monkeypatch):
    sent = []

    async def fake_desktop(title, body, subtitle=""):
        sent.append(("desktop", title))
        return "shown"

    async def fake_imsg(handle, text):
        sent.append(("imessage", handle))
        return "sent"

    import personal_agentic.attention as att

    monkeypatch.setattr(att, "desktop_notification", fake_desktop)
    monkeypatch.setattr(att, "send_imessage", fake_imsg)
    aid = agent.attention.open_review("main", "EOD summary", "body")
    item = agent.store.get_attention(aid)
    await agent.attention.escalate_due(now=item["ts"] + 60)
    assert sent == []
    await agent.attention.escalate_due(now=item["ts"] + 21 * 60)
    assert sent == [("desktop", "Agent: waiting on you")]
    await agent.attention.escalate_due(now=item["ts"] + 95 * 60)
    assert sent[-1] == ("imessage", "+15555550100")
    await agent.attention.escalate_due(now=item["ts"] + 500 * 60)
    assert len(sent) == 2


async def test_mcp_stdio_server(agent, cfg):
    Path(cfg.paths.mcp_config).write_text(
        json.dumps(
            {"mcpServers": {"fake": {"command": sys.executable, "args": [str(HERE / "fake_mcp_server.py")]}}}
        )
    )
    await agent.mcp.start(wait=30)
    status = agent.mcp.status()
    assert status[0]["status"] == "connected", status
    names = {t.name for t in agent.tools.all() if t.origin == "mcp:fake"}
    assert names == {"fake__search_items", "fake__send_message", "fake__crash"}
    ctx = ToolContext(agent, "main")
    assert decide(agent.tools.get("fake__send_message"), {}, ctx) == "ask"
    assert decide(agent.tools.get("fake__search_items"), {}, ctx) == "allow"
    reply = await agent.submit('CALL fake__search_items {"query": "widgets"}', source="web")
    assert "found 2 items for widgets" in reply
    await agent.mcp.stop()
    assert not [t for t in agent.tools.all() if t.origin == "mcp:fake"]


async def test_yes_answers_pending_approval(agent):
    async def handler(args, ctx):
        return "sent"

    agent.tools.register(
        ToolSpec(
            name="mail__send_email",
            description="",
            parameters={"type": "object"},
            handler=handler,
            origin="mcp:mail",
        )
    )
    fut = agent.submit('CALL mail__send_email {"to": "dana@example.com"}', source="terminal")
    for _ in range(100):
        if agent.store.open_attention(kind="approval"):
            break
        await asyncio.sleep(0.02)
    quick = agent.submit("yes", source="terminal")
    assert quick.done()
    assert (await fut) == "Done. sent"
    # with nothing pending, "yes" is just a message
    assert (await agent.submit("yes", source="terminal")) == "Echo: yes"


async def test_scheduler_tick_runs_due_and_skips_missed(agent, cfg):
    from datetime import datetime, timedelta

    (Path(cfg.paths.routines_dir) / "tick.md").write_text(
        "---\nschedule: {every: 15m}\ncontext: isolated\n---\nping\n"
    )
    agent.routines.load()
    now = datetime.now(agent.routines.tz)
    agent.routines.next_runs["tick"] = now - timedelta(minutes=1)
    await agent.routines.tick(now)
    task = agent.routines.running.get("tick")
    assert task is not None and (await task) == "Echo: ping"
    agent.routines.next_runs["tick"] = now - timedelta(hours=2)
    await agent.routines.tick(now)
    assert "tick" not in agent.routines.running
    assert agent.store.recent_runs("tick")[0]["status"] == "skipped"
    assert agent.routines.next_runs["tick"] > now


async def test_stop_during_tool_call_keeps_history_valid(agent):
    started = asyncio.Event()

    async def slow(args, ctx):
        started.set()
        await asyncio.sleep(30)
        return "never"

    agent.tools.register(
        ToolSpec(name="slow_tool", description="", parameters={"type": "object"}, handler=slow)
    )
    fut = agent.submit("CALL slow_tool {}", source="web")
    await asyncio.wait_for(started.wait(), 5)
    agent.stop_current()
    assert await fut == "(stopped)"
    roles = [m["role"] for m in agent.main.messages]
    assert roles[-2:] == ["assistant", "tool"]
    assert agent.main.messages[-1]["content"].startswith("[stopped")
    assert (await agent.submit("next one", source="web")) == "Echo: next one"
    roles = [m["role"] for m in agent.main.messages]
    assert roles[-4:] == ["assistant", "tool", "user", "assistant"]


async def test_stop_while_waiting_for_approval_expires_it(agent):
    async def handler(args, ctx):
        return "x"

    agent.tools.register(
        ToolSpec(
            name="crm__delete_record",
            description="",
            parameters={"type": "object"},
            handler=handler,
            origin="mcp:crm",
        )
    )
    fut = agent.submit("CALL crm__delete_record {}", source="web")
    for _ in range(100):
        if agent.store.open_attention(kind="approval"):
            break
        await asyncio.sleep(0.02)
    agent.stop_current()
    assert await fut == "(stopped)"
    assert not agent.store.open_attention()
    # a later "ok" is a normal message, not an approval
    assert (await agent.submit("ok", source="web")) == "Echo: ok"


async def test_task_waiting_on_main_from_main_turn_errors_instead_of_hanging(agent, cfg):
    (Path(cfg.paths.tasks_dir) / "loop.py").write_text(
        "from personal_agentic import task\n\n"
        "@task(description='calls back into main')\n"
        "async def loopy(ctx):\n"
        "    return await ctx.ask_main('hello?')\n"
    )
    (Path(cfg.paths.tasks_dir) / "inline.md").write_text(
        "---\ncontext: main\n---\nDo the inline thing for {{input}}\n"
    )
    agent.routines.load()
    reply = await asyncio.wait_for(agent.submit('CALL run_task {"name": "loopy"}', source="web"), 10)
    assert "ask_main() can't be used here" in reply
    # a main-context Markdown task hands its instructions back instead of queueing
    reply = await asyncio.wait_for(
        agent.submit('CALL run_task {"name": "inline", "input": "Q4"}', source="web"), 10
    )
    assert "carry these out now" in reply and "inline thing for Q4" in reply
    # started on its own (not from a main turn), ask_main works
    out = await asyncio.wait_for(agent.routines.trigger("loopy"), 10)
    assert out.startswith("Echo:")


async def test_review_not_resolved_by_scripts_or_commands(agent):
    aid = agent.attention.open_review("main", "EOD", "body")
    await agent.submit("build finished", source="nightly")
    await agent.submit("/status", source="web")
    assert agent.store.get_attention(aid)["status"] == "open"
    await agent.submit("looks fine", source="web")
    assert agent.store.get_attention(aid)["status"] == "resolved"


async def test_mcp_reconnects_after_server_dies(agent, cfg, monkeypatch):
    import personal_agentic.mcp_servers as ms

    monkeypatch.setattr(ms, "PING_EVERY", 0.3)
    monkeypatch.setattr(ms, "RECONNECT_FIRST", 0.3)
    Path(cfg.paths.mcp_config).write_text(
        json.dumps(
            {"mcpServers": {"fake": {"command": sys.executable, "args": [str(HERE / "fake_mcp_server.py")]}}}
        )
    )
    await agent.mcp.start(wait=30)
    server = agent.mcp.servers["fake"]
    assert server.status == "connected"
    first_client = server.client
    try:
        await asyncio.wait_for(server.client.call_tool("crash", {}), 5)
    except Exception:
        pass
    for _ in range(200):
        if server.status == "connected" and server.client is not None and server.client is not first_client:
            break
        await asyncio.sleep(0.05)
    assert server.status == "connected" and server.client is not first_client
    assert agent.tools.get("fake__search_items") is not None
    await agent.mcp.stop()
