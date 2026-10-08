from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from personal_agentic.config import WorkdayConfig, parse_duration
from personal_agentic.llm.client import LLMClient, ThinkSplitter, split_reasoning
from personal_agentic.memory import chunk_text
from personal_agentic.routines.loader import split_front_matter
from personal_agentic.routines.schedule import parse_schedule
from personal_agentic.store import Store
from personal_agentic.tools.base import name_words, schema_from_function, tool
from personal_agentic.working_context import WorkingContext

TZ = ZoneInfo("America/Chicago")
WD = WorkdayConfig(holidays=[date(2026, 11, 26)])


def at(y, mo, d, hh, mm):
    return datetime(y, mo, d, hh, mm, tzinfo=TZ)


def test_durations():
    assert parse_duration("15m").total_seconds() == 900
    assert parse_duration("2h").total_seconds() == 7200
    assert parse_duration(30).total_seconds() == 30
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_schedule_at_workdays_skips_weekend_and_holiday():
    s = parse_schedule({"at": "16:00", "days": "workdays"}, WD)
    # Friday 2026-10-09 after 4pm -> Monday 2026-10-12 16:00
    assert s.next_after(at(2026, 10, 9, 17, 0)) == at(2026, 10, 12, 16, 0)
    assert s.next_after(at(2026, 10, 8, 9, 0)) == at(2026, 10, 8, 16, 0)
    # Thanksgiving is a holiday -> Friday
    assert s.next_after(at(2026, 11, 25, 17, 0)) == at(2026, 11, 27, 16, 0)


def test_schedule_yaml_unquoted_time():
    meta, _ = split_front_matter("---\nschedule: {at: 8:30}\n---\nbody")
    s = parse_schedule(meta["schedule"], WD)
    assert s.next_after(at(2026, 10, 8, 7, 0)) == at(2026, 10, 8, 8, 30)


def test_schedule_every_in_window():
    s = parse_schedule({"every": "15m", "window": "workday"}, WD)
    assert s.next_after(at(2026, 10, 8, 7, 0)) == at(2026, 10, 8, 8, 0)
    assert s.next_after(at(2026, 10, 8, 8, 0)) == at(2026, 10, 8, 8, 15)
    assert s.next_after(at(2026, 10, 8, 9, 7)) == at(2026, 10, 8, 9, 15)
    # after the window closes -> next workday morning (Fri 17:20 -> Mon 08:00)
    assert s.next_after(at(2026, 10, 9, 17, 20)) == at(2026, 10, 12, 8, 0)
    assert "every 15 min" in s.describe()


def test_schedule_cron():
    s = parse_schedule("30 8 * * 1-5", WD)
    assert s.next_after(at(2026, 10, 10, 12, 0)) == at(2026, 10, 12, 8, 30)
    with pytest.raises(ValueError):
        parse_schedule("not a cron", WD)


def test_think_splitter_streams_reasoning_separately():
    sp = ThinkSplitter()
    out = []
    for piece in ["<thi", "nk>abc", "def</th", "ink>\nHel", "lo"]:
        out += sp.feed(piece)
    out += sp.flush()
    reasoning = "".join(t for k, t in out if k == "reasoning")
    content = "".join(t for k, t in out if k == "content")
    assert reasoning == "abcdef"
    assert content == "Hello"


def test_split_reasoning_bare_close_tag():
    assert split_reasoning("thinking here</think>\n\nAnswer") == ("Answer", "thinking here")
    assert split_reasoning("Just an answer") == ("Just an answer", "")


def test_working_context_updates(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    wc = WorkingContext(tmp_path / "wc.md", store)
    wc.update("Plate", "append", "- [ ] Review PR #12", author="t")
    wc.update("Plate", "append", "- [ ] Reply to Sam", author="t")
    text = wc.read()
    assert text.index("Review PR #12") < text.index("Reply to Sam") < text.index("## In progress")
    wc.update("Plate", "check", "pr #12", author="t")
    assert "- [x] Review PR #12" in wc.read()
    wc.update("Plate", "remove", "sam", author="t")
    assert "Reply to Sam" not in wc.read()
    wc.update("Daily log", "prepend", "### 2026-10-08\n- did things", author="t")
    log_part = wc.read().split("## Daily log")[1]
    assert log_part.index("<!--") < log_part.index("### 2026-10-08")
    assert "No section" in wc.update("Nope", "remove", "x", author="t")
    wc.update("Projects", "append", "- Apollo", author="t")
    assert "## Projects\n- Apollo" in wc.read()
    assert len(store.wc_versions()) >= 6


def test_tool_decorator_schema():
    @tool(policy="allow")
    async def lookup(name: str, limit: int = 5, tags: list[str] | None = None, ctx=None) -> str:
        """Find someone.

        Args:
            name: Who to find.
        """
        return name

    schema = schema_from_function(lookup)
    assert schema["required"] == ["name"]
    assert schema["properties"]["limit"] == {"type": "integer"}
    assert schema["properties"]["tags"] == {"type": "array", "items": {"type": "string"}}
    assert schema["properties"]["name"]["description"] == "Who to find."
    assert "ctx" not in schema["properties"]
    assert lookup.__pa_tool__["description"] == "Find someone."


def test_name_words():
    assert {"send", "message"} <= name_words("sendMessage")
    assert {"post", "message"} <= name_words("slack__post_message")


def test_chunk_text():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = chunk_text(text, size=1000, overlap=100)
    assert all(len(c) <= 1000 for c in chunks)
    assert chunks[0].startswith("line 0") and "line 199" in chunks[-1]


async def test_llm_client_stream_and_tools(cfg):
    llm = LLMClient(cfg.llm)
    deltas = []
    r = await llm.chat(
        [{"role": "user", "content": "hello there"}], on_delta=lambda k, t: deltas.append((k, t))
    )
    assert r.content == "Echo: hello there"
    assert "".join(t for _, t in deltas) == "Echo: hello there"
    assert r.usage and r.usage["prompt_tokens"] > 0

    tools = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]
    r = await llm.chat([{"role": "user", "content": 'CALL f {"a": 1}\nCALL g {"b": [1, 2]}'}], tools=tools)
    assert [(t.name, t.arguments) for t in r.tool_calls] == [("f", '{"a": 1}'), ("g", '{"b": [1, 2]}')]

    r = await llm.chat([{"role": "user", "content": "THINK about it"}], thinking=True)
    assert r.reasoning == "Let me ponder this carefully." and r.content == "The answer is 42."
    assert await llm.context_window() == 131072
    await llm.aclose()


def test_repo_routines_and_tasks_load():
    from pathlib import Path

    from personal_agentic.routines.loader import load_folder

    root = Path(__file__).resolve().parents[1]
    r, r_err = load_folder(root / "routines", "routine", WD)
    t, t_err = load_folder(root / "tasks", "task", WD)
    assert not r_err and not t_err
    names = {d.name for d in r}
    assert {"end-of-day-summary", "morning-priorities", "pr-watch"} <= names
    eod = next(d for d in r if d.name == "end-of-day-summary")
    assert eod.context == "main" and eod.await_reply and eod.catch_up == "2h"
    assert eod.schedule.next_after(at(2026, 10, 8, 12, 0)) == at(2026, 10, 8, 16, 0)
    pr = next(d for d in r if d.name == "pr-watch")
    assert pr.report == "changes" and pr.vars["min_open_hours"] == 3
    assert "h1, h2, h3" in pr.render("Harper", at(2026, 10, 8, 9, 0))
    assert t[0].name == "status-update"


def test_example_python_routine_loads():
    from pathlib import Path

    from personal_agentic.routines.loader import load_folder

    root = Path(__file__).resolve().parents[1]
    defs, errs = load_folder(root / "examples" / "routines", "routine", WD)
    assert not errs and defs[0].name == "pr-watch-py" and defs[0].python_fn and not defs[0].enabled


async def test_llm_falls_back_when_extras_rejected(cfg, fake_lm):
    fake_lm["app"].state.reject_extras = True
    try:
        llm = LLMClient(cfg.llm)
        r = await llm.chat([{"role": "user", "content": "hi"}])
        assert r.content == "Echo: hi"
        assert "chat_template_kwargs" not in fake_lm["app"].state.requests[-1]
        await llm.aclose()
    finally:
        fake_lm["app"].state.reject_extras = False


def test_missed_run_grace():
    from datetime import timedelta

    from personal_agentic.routines.defs import RoutineDef
    from personal_agentic.routines.manager import _grace

    assert _grace(RoutineDef(name="a", kind="routine")) == timedelta(minutes=15)
    assert _grace(RoutineDef(name="a", kind="routine", catch_up="3h")) == timedelta(hours=3)
    assert _grace(RoutineDef(name="a", kind="routine", catch_up=True)) > timedelta(days=365)
