# personal-agentic

A personal work agent that runs on your Mac. It talks to a local model in LM Studio,
uses your work tools through MCP servers, keeps one long-running conversation plus a
working-context file of what you have going on, and runs routines on a schedule.

You can talk to it from a web page, a terminal, or your own scripts. They all share
one live feed, so whatever you start in the terminal also shows up on the web page.

## First run

```bash
cd ~/personal_agentic
brew install uv            # if you don't have it
uv sync                    # creates .venv with Python 3.14 and installs everything
uv run pa init             # creates config.toml, mcp.json, .env and ~/.personal_agentic/
```

In LM Studio:
1. Load `qwen/qwen3.8-27b` with its context length set (you have 262144).
2. Load the nomic text embedding model, plus the code one if you want it.
3. Start the local server (Developer tab, or `lms server start`). It should be on port 1234.

Then:

```bash
uv run pa check            # tests the model, tool calling, thinking, embeddings, MCP servers
uv run pa serve            # starts the agent; web page at http://127.0.0.1:8765
uv run pa chat             # in another terminal
```

`pa check` prints the model IDs LM Studio reports. If they don't match the defaults, put
the right ones in `config.toml` (`[llm] model`, `[embeddings.text] model`). Also set
`[notify] imessage_self` to your phone number or Apple ID, so it can message you.

To use `pa` from any folder (for example, from scripts in other repos):

```bash
alias pa="uv run --project ~/personal_agentic pa"
```

### macOS permissions

The first time it sends an alert or an iMessage, macOS asks for permission:
- **Desktop alerts** come from Script Editor. Allow its notifications in System Settings > Notifications.
- **iMessages** need your terminal app allowed to control Messages, in System Settings > Privacy & Security > Automation.
  Messages must be signed in on this Mac.

## Talking to it

| From | How |
|---|---|
| Web page | http://127.0.0.1:8765: the conversation, things waiting on you, working context, activity, routines |
| Terminal | `pa chat`: the same feed, live |
| One-off | `pa ask "what's on my plate?"` prints the reply |
| Scripts | `pa log "export done" --source nightly`, or pipe output into `pa log` |
| Commands | `pa run -- pytest -x` runs the command and streams its output to the feed. Add `--ask "why did it fail?"` to have the agent look at the output when it finishes |
| Python | `from personal_agentic.client import AgentClient`, then `AgentClient(source="my-script").ask(...)` / `.log(...)` |

Every message is labeled with where it came from (terminal, web, a script's name, a
routine's name), so the web page is the one place to watch everything.

Chat commands work in both the terminal and the web page:
- `/think [msg]` and `/fast [msg]` switch thinking on or off for chat, or for just one message.
- `/task NAME input` runs a task. `/run NAME` runs a routine now.
- `/compact` summarizes older conversation now. `/status` shows the current state. `/help` lists the commands.

The terminal also has:
- `/approve [id] [note]` and `/deny [id] [note]`. A plain "yes" or "no" also answers a single pending approval.
- `/stop` to stop the current turn, `/wc` to print the working context, `/quit` to exit.

## How it keeps track of things

- **One conversation.** Everything goes into one ongoing conversation: your messages from
  any client, routines that run in the main context, and notes from background work.
  Messages are handled one at a time, in order.
- **Compaction.** When the conversation passes `context.compact_at_tokens` (100k by default),
  the oldest part is summarized. The originals go into long-term memory, and the agent can
  search them with `recall`. 100k is well below your 262k window on purpose: re-processing a
  very long prompt on a Mac is slow. Raise it if you find it summarizing too often.
- **Working context** (`~/.personal_agentic/working_context.md`). This is your Plate, In progress,
  Waiting on others, Daily log, and Notes. The agent reads and edits it. You can edit it by
  hand or on the web page, and every version is kept. When it changes outside the
  conversation, the agent sees a diff with your next message.
- **Memory.** `remember` and `recall` use nomic embeddings stored in SQLite
  (`~/.personal_agentic/agent.db`).
- **Sub-agents.** For self-contained work, the agent can start a sub-agent with its own fresh
  context (`spawn_agent`), either waiting for the result or in the background. Results come
  back into the main conversation, and `agent_result` reads a finished one. The web page's
  Activity tab shows each sub-agent's full transcript.

## Routines and tasks

Routines live in `routines/`. Tasks (run when you ask) live in `tasks/`. Each one is a
Markdown file: YAML settings at the top, then the prompt. Edits are picked up within
30 seconds, with no restart.

```markdown
---
description: "What it does"
schedule: {at: "16:00", days: workdays}     # or {every: 15m, window: workday} or "30 8 * * 1-5"
context: main          # main: runs in the one conversation. isolated: a sub-agent runs it
thinking: true
tools: ["slack__*", "working_context_*"]    # isolated only
report: changes        # isolated only: always | changes (stay quiet on NO_UPDATE) | never
notify: [desktop]      # also alert here when it finishes (the web page always gets it)
await_reply: true      # it expects your answer; escalates if you don't respond
escalate: [{after: 30m, via: desktop}, {after: 2h, via: imessage}]
catch_up: 2h           # still run if the Mac wakes up within 2h of the scheduled time
vars: {repo: "org/x"}  # use as {{repo}} in the prompt
enabled: true
---
The prompt. {{user_name}}, {{today}}, {{now}}, {{weekday}} and {{input}} are filled in.
```

"Workdays" and the workday window come from `[workday]` in config.toml, and skip holidays.

Your three routines are included. All three are **off** (`enabled: false`) until their
MCP servers are set up:

| Routine | When | How |
|---|---|---|
| `end-of-day-summary` | 4:00pm workdays | Runs in the main conversation. Sub-agents gather Slack, email, Docs and company tools; it posts a summary and waits for your OK or edits, then logs the result to the Daily log. Escalates if you don't answer. |
| `morning-priorities` | 8:30am workdays | Runs in the main conversation. Reads the working context, checks tools for updates, updates the Plate, and gives you a prioritized list. |
| `pr-watch` | every 15 min, 8:00–17:30 | Runs as an isolated sub-agent with thinking off. PRs in `vars.repo` from `vars.authors` that have been open 3h+ and aren't mentioned in Slack go on your Plate. Quiet when there's nothing new. Fill in `vars` first. |

You can try any of them without waiting for the schedule: `pa routines run morning-priorities`,
or click Run now on the web page. A manual run works even when a routine is off.

**Python routines and tasks** are for logic. Put a `.py` file in `routines/` or `tasks/`:

```python
from personal_agentic import routine

@routine(every="15m", window="workday", report="changes")
async def my_watch(ctx):
    data = await ctx.call_tool("github__search_pull_requests", {"query": "..."})  # any tool, approvals apply
    answer = await ctx.ask("Is any of this urgent?", tools=["slack__*"])         # a sub-agent
    await ctx.working_context.update("Plate", "append", "- [ ] ...")
    await ctx.notify("Heads up", via=["desktop"], needs_reply=False)
    ctx.state["seen"] = [...]          # persists between runs
    return "summary"                   # or "NO_UPDATE" / None to stay quiet
```

`ctx` also has `ask_main()`, `remember()`, `recall()`, `emit()` (a progress line in the
feed), `now()`, `vars` and `log`. See `examples/routines/pr_watch.py` for a full one.
One limit: a Python task started by the agent itself (through `run_task`) shouldn't call
`ask_main()`, because the main conversation is busy waiting on that task.

## Approvals and escalation

Reading is free. Tools that change things other people see ask you first: on the web page
(the yellow "Waiting on you" box), in the terminal, or with a plain "yes" or "no" reply.
For MCP tools, the default (`[tools] mcp_default = "auto"`) uses each tool's read-only and
destructive hints, then its name. Names with words like send, post, create, update or
delete ask; everything else runs. You can override this:
- in `config.toml`: `[tools] rules = [{ match = "slack__search_*", action = "allow" }, ...]`
- per server in mcp.json: `"policy": {"send_*": "ask", "*": "allow"}`

iMessages to yourself and desktop alerts never ask. iMessages to anyone else always ask.

Anything waiting on you shows on the web page right away. If you don't answer, it escalates
on the `[notify] default_escalation` schedule (a desktop alert after 20 min, an iMessage after
90 min), or on a routine's own `escalate`. By default this only happens during the workday.

## MCP servers

### Bring over what you use in Claude Code

```bash
pa mcp import            # reads ~/.claude.json: user-scoped and local-scoped servers
pa mcp import --dry-run  # just show what it would add
pa mcp import --project ~/some/repo   # also that folder's .mcp.json
```

This copies the server definitions into `mcp.json` and keeps any entry you already have
there (`--overwrite` replaces them). Logins don't carry over: Claude Code's tokens live in its
keychain entries and were issued to its own app. So the import lists the servers to sign in to.

### Sign in

```bash
pa mcp login atlassian   # opens your browser, like /mcp in Claude Code
pa mcp list              # every server, whether you're signed in, and what the agent sees
pa mcp logout atlassian
```

You can also click **Sign in** next to a server in the web page's System tab. Tokens are saved
in `~/.personal_agentic/oauth/<server>.json`, readable only by you, and refreshed automatically.
The agent never opens a browser by itself. If a server needs a fresh sign-in, it shows as
"needs sign-in" and the web page tells you.

Any `http` or `sse` server without an `Authorization` header uses sign-in.

- **Servers that register apps on their own** (Atlassian; Glean in most setups) need nothing
  more than the URL.
- **Servers that don't** (Slack; Google's Workspace servers) need an app you register with
  them. Give its client ID and, if it has one, its secret:

```json
"slack": {
  "type": "http",
  "url": "https://mcp.slack.com/mcp",
  "oauth": {
    "clientId": "${SLACK_CLIENT_ID}",
    "clientSecret": "${SLACK_CLIENT_SECRET}",
    "callbackPort": 33418,
    "scopes": "search:read.public channels:history"
  }
}
```

  The app's allowed redirect URL has to be `http://localhost:33418/callback`, or the
  `callbackPort` you choose.

### Your services

| Service | How to connect |
|---|---|
| Atlassian | Sign in. Or, if your admin allows API tokens: `"url": "https://mcp.atlassian.com/v2/mcp"` with `"headers": {"Authorization": "Basic ${ATLASSIAN_BASIC}"}` (base64 of `email:token`). Code search and Teams tools need sign-in. |
| Glean | Sign in, with the server URL from Glean's MCP Configurator (it's specific to your company). |
| Slack | Needs an internal Slack app in your workspace (Slack only allows directory-listed or internal apps, and admins approve them). Then sign in with its client ID and secret, or put a user token from that app in an `Authorization: Bearer` header. |
| Google | Claude's built-in Gmail/Calendar/Drive connectors run through Anthropic, so the agent can't use them. Google's own Workspace MCP servers (e.g. `https://gmailmcp.googleapis.com/mcp/v1`) are a developer preview: you need to be enrolled, create an OAuth client in a Google Cloud project with the `http://localhost:33418/callback` redirect, and enable the APIs. Then sign in with that client ID and secret. |

### Everything else

- Local servers run as commands: `{"command": "npx", "args": ["-y", "some-mcp"], "env": {"TOKEN": "${TOKEN}"}}`.
- `${NAME}` values come from `.env` (git-ignored).
- Tools show up as `<server>__<tool>`. `pa tools` lists them.
- Each server can also set:
  - `include_tools` and `exclude_tools`
  - `policy`
  - `timeout` (in seconds)
  - `headersHelper`, a command that prints JSON headers
  - `disabled`
- Server logs go to `~/.personal_agentic/logs/mcp-<name>.log`.
- After editing mcp.json, restart the servers from the System tab.

## Your own tools

Any function marked `@tool` in a `tools/*.py` file becomes a tool. Its parameters come from
the type hints, and its description comes from the docstring. See `examples/tools/`.

```python
from personal_agentic import tool

@tool(policy="allow", read_only=True)
async def lookup_employee(name: str) -> str:
    """Find someone in the company directory."""
```

## Thinking mode

With thinking on, the model reasons before each step. It's better at planning and judgment,
but much slower, because every step of a tool loop thinks again. The defaults are: off for
chat, on for routines and sub-agents, and set per routine with `thinking:`. Reasoning shows
collapsed on the web page and isn't kept in the conversation.

`pa check` shows whether thinking can be switched on and off. If it can't, change
`[llm] thinking_control` (`chat_template_kwargs`, `reasoning_effort`, `both`, or `none`). If
LM Studio rejects those extra request fields, the agent automatically falls back to plain
OpenAI requests.

## Performance notes

- LM Studio reuses its cache for the unchanged start of a prompt. To keep that start
  unchanged, the system prompt never changes, and time and source go in each message instead.
- `max_parallel = 1` sends one request to LM Studio at a time. Chat jumps ahead of routines,
  and routines jump ahead of background sub-agents. If background work (like the 15-minute
  PR check) slows chat down, load a second model instance in LM Studio and raise `max_parallel`.

## Layout

```
src/personal_agentic/
  app.py              the running agent: main queue, turns, sub-agents, slash commands
  agent/              tool-calling loop, sessions, compaction
  llm/                LM Studio chat (streaming, tools, reasoning), embeddings, priority gate
  mcp_servers.py      MCP connections (stdio, HTTP, SSE)
  oauth.py            browser sign-in for MCP servers, saved tokens
  mcp_import.py       pa mcp import (from Claude Code)
  tools/              registry and approval policy, built-in tools, @tool loader
  routines/           Markdown/Python loader, schedules, runner, ctx for Python routines
  attention.py        approvals, reviews, escalation
  working_context.py  the working-context file
  memory.py           long-term memory and search
  notify.py           macOS alerts and iMessage
  server/             FastAPI + websocket + the web page
  terminal.py, cli.py, client.py
routines/ tasks/ tools/ prompts/    yours to edit
examples/                           a Python routine and custom tools to copy from
```

Data lives in `~/.personal_agentic/`: `agent.db` (conversation, events, memory, state),
`working_context.md`, and `logs/`.

## Development

```bash
uv run pytest            # runs against a fake LM Studio and a fake MCP server; no model needed
uv run ruff check src tests
```
