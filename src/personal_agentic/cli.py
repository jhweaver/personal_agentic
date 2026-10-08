"""`pa`: run the agent and talk to it.

pa init                      set up config files and the data folder
pa check                     test LM Studio, embeddings and MCP servers
pa serve                     run the agent (web page at http://127.0.0.1:8765)
pa chat                      terminal chat (mirrors the web page)
pa ask "question"            one-shot question, prints the reply
pa log "text"                post a line to the feed (or pipe text in)
pa run -- <command>          run a command, streaming its output to the feed
pa routines [run NAME]       list routines and tasks, or run one now
pa task NAME [input]         run a task
pa status | pa tools | pa wc
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

import httpx

from .client import DEFAULT_URL
from .config import load_config


def _cfg(args: argparse.Namespace):
    return load_config(args.config)


def _url(args: argparse.Namespace) -> str:
    if getattr(args, "url", None):
        return args.url.rstrip("/")
    if os.environ.get("PA_URL"):
        return os.environ["PA_URL"].rstrip("/")
    try:
        return _cfg(args).server_url
    except Exception:
        return DEFAULT_URL


def _http(args: argparse.Namespace, timeout: float | None = 30.0) -> httpx.Client:
    return httpx.Client(base_url=_url(args), timeout=timeout)


def _die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    sys.exit(code)


def _server_call(args: argparse.Namespace, method: str, path: str, **kw):
    try:
        with _http(args, timeout=kw.pop("timeout", 30.0)) as http:
            r = http.request(method, path, **kw)
            r.raise_for_status()
            return r.json()
    except httpx.ConnectError:
        _die(f"Can't reach the agent at {_url(args)}. Start it with `pa serve`.")
    except httpx.HTTPStatusError as e:
        _die(f"{e.response.status_code}: {e.response.text}")


# ---- commands --------------------------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> None:
    root = Path.cwd()
    copies = [
        ("config.example.toml", "config.toml"),
        ("mcp.example.json", "mcp.json"),
        (".env.example", ".env"),
    ]
    for src, dst in copies:
        s, d = root / src, root / dst
        if d.exists():
            print(f"  exists   {dst}")
        elif s.exists():
            shutil.copy(s, d)
            print(f"  created  {dst}")
        else:
            print(f"  missing  {src} (run `pa init` from the repo folder)")
    cfg = load_config(root / "config.toml" if (root / "config.toml").exists() else None)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    from .store import Store
    from .working_context import WorkingContext

    store = Store(cfg.db_path)
    wc = WorkingContext(cfg.resolve(cfg.paths.working_context), store)
    store.close()
    print(f"  data     {cfg.data_dir}")
    print(f"  working context  {wc.path}")
    print("\nNext: edit config.toml (model names, your iMessage handle), then `pa check`, then `pa serve`.")


def cmd_check(args: argparse.Namespace) -> None:
    asyncio.run(_check(_cfg(args), args.skip_mcp))


async def _check(cfg, skip_mcp: bool) -> None:
    from .llm import Embedder, EmbeddingError, LLMClient, LLMError

    ok = True

    def line(status: bool | None, label: str, detail: str = "") -> None:
        nonlocal ok
        mark = {True: "\033[32m✓\033[0m", False: "\033[31m✗\033[0m", None: "\033[33m•\033[0m"}[status]
        if status is False:
            ok = False
        print(f"{mark} {label}{(': ' + detail) if detail else ''}")

    print(f"config: {cfg.config_file or '(defaults)'}\n")
    llm = LLMClient(cfg.llm)
    try:
        models = await llm.list_models()
        ids = [m.get("id") for m in models]
        line(True, "LM Studio reachable", cfg.llm.base_url)
        line(
            cfg.llm.model in ids,
            f"chat model {cfg.llm.model}",
            "available" if cfg.llm.model in ids else f"not listed; have: {', '.join(map(str, ids))}",
        )
        window = await llm.context_window(refresh=True)
        line(window >= 32768, "context window", f"{window:,} tokens")
        t0 = time.time()
        resp = await llm.chat(
            [{"role": "user", "content": "Reply with just the word OK."}], thinking=False, max_tokens=200
        )
        dt = time.time() - t0
        line(bool(resp.content), "chat (thinking off)", f"{resp.content[:40]!r} in {dt:.1f}s")
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_time",
                    "description": "Get the current time",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        resp = await llm.chat(
            [{"role": "user", "content": "What time is it? Use the tool."}],
            tools=tools,
            thinking=False,
            max_tokens=400,
        )
        line(
            bool(resp.tool_calls),
            "tool calling",
            resp.tool_calls[0].name if resp.tool_calls else f"no tool call; said {resp.content[:60]!r}",
        )
        t0 = time.time()
        resp = await llm.chat(
            [{"role": "user", "content": "What is 17 * 23? Answer with just the number."}],
            thinking=True,
            max_tokens=4000,
        )
        line(
            bool(resp.reasoning) or None,
            "thinking on",
            f"{len(resp.reasoning)} chars of reasoning, answer {resp.content[:20]!r}, {time.time() - t0:.1f}s"
            + ("" if resp.reasoning else " (no separate reasoning seen; see thinking_control in config)"),
        )
    except (LLMError, httpx.HTTPError) as e:
        line(False, "LM Studio", str(e))
    finally:
        await llm.aclose()

    emb = Embedder(cfg.embeddings, cfg.llm.base_url, cfg.llm.api_key)
    for space in emb.available_spaces():
        try:
            vec = await emb.embed(["hello world"], space=space)  # type: ignore[arg-type]
            line(True, f"embeddings ({space})", f"{getattr(cfg.embeddings, space).model}, dim {vec.shape[1]}")
        except EmbeddingError as e:
            line(None if space == "code" else False, f"embeddings ({space})", str(e)[:200])
    await emb.aclose()

    if not skip_mcp:
        from .mcp_servers import load_mcp_config

        servers = load_mcp_config(cfg.resolve(cfg.paths.mcp_config))
        if not servers:
            line(None, "MCP servers", f"none configured in {cfg.paths.mcp_config}")
        else:
            from .app import AgentApp

            app = AgentApp(cfg)
            await app.mcp.start(wait=45)
            for s in app.mcp.status():
                line(
                    s["status"] == "connected",
                    f"MCP {s['name']}",
                    f"{s['tools']} tools" if s["status"] == "connected" else (s["error"] or s["status"]),
                )
            await app.mcp.stop()
            app.store.close()
    print("\nAll good." if ok else "\nSome checks failed (see above).")


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .server.api import create_app

    cfg = _cfg(args)
    log_dir = cfg.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    from rich.logging import RichHandler

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            RichHandler(rich_tracebacks=True, show_path=False),
            logging.FileHandler(log_dir / "agent.log"),
        ],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    print(f"Agent web page: {cfg.server_url}   (terminal: `pa chat`)")
    if args.open:
        import webbrowser

        webbrowser.open(cfg.server_url)
    app = create_app(cfg, scheduler=not args.no_scheduler, mcp=not args.no_mcp)
    uvicorn.run(
        app, host=cfg.server.host, port=cfg.server.port, log_level="warning", ws_max_size=16 * 1024 * 1024
    )


def cmd_chat(args: argparse.Namespace) -> None:
    from .terminal import TerminalChat

    try:
        asyncio.run(TerminalChat(_url(args), source=args.source, show_reasoning=args.reasoning).run())
    except KeyboardInterrupt:
        pass


def cmd_ask(args: argparse.Namespace) -> None:
    text = " ".join(args.text) if args.text else sys.stdin.read()
    if not text.strip():
        _die("Nothing to ask.")
    thinking = True if args.think else (False if args.fast else None)
    res = _server_call(
        args,
        "POST",
        "/api/message",
        json={"text": text, "source": args.source, "wait": True, "thinking": thinking},
        timeout=None,
    )
    print(res.get("reply", ""))


def cmd_log(args: argparse.Namespace) -> None:
    text = " ".join(args.text) if args.text else sys.stdin.read()
    _server_call(args, "POST", "/api/log", json={"text": text, "source": args.source, "level": args.level})


def cmd_run(args: argparse.Namespace) -> None:
    command = args.command[1:] if args.command and args.command[0] == "--" else args.command
    if not command:
        _die("Usage: pa run [--name NAME] [--ask QUESTION] -- <command> [args...]")
    sys.exit(asyncio.run(_run(args, command)))


async def _run(args: argparse.Namespace, command: list[str]) -> int:
    name = args.name or Path(command[0]).name
    url = _url(args)
    buf: list[tuple[str, str]] = []
    tail: list[str] = []
    reachable = True

    async with httpx.AsyncClient(base_url=url, timeout=10) as http:

        async def post(text: str, stream: str | None = None, level: str = "info") -> None:
            nonlocal reachable
            if not reachable:
                return
            try:
                await http.post(
                    "/api/log", json={"text": text, "source": name, "stream": stream, "level": level}
                )
            except httpx.HTTPError:
                reachable = False
                print(f"[pa] agent not reachable at {url}; output stays local", file=sys.stderr)

        async def flusher() -> None:
            while True:
                await asyncio.sleep(0.5)
                await flush()

        async def flush() -> None:
            if not buf:
                return
            chunk = list(buf)
            buf.clear()
            for stream in ("stdout", "stderr"):
                lines = [t for s, t in chunk if s == stream]
                if lines:
                    await post("\n".join(lines), stream=stream)

        async def pump(reader: asyncio.StreamReader, stream: str, sink) -> None:
            while line := await reader.readline():
                text = line.decode(errors="replace").rstrip("\n")
                sink.write(text + "\n")
                sink.flush()
                buf.append((stream, text))
                tail.append(text)
                del tail[:-200]

        await post(f"$ {' '.join(command)}", level="info")
        start = time.time()
        proc = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1 << 24
        )
        f = asyncio.create_task(flusher())
        assert proc.stdout and proc.stderr
        await asyncio.gather(pump(proc.stdout, "stdout", sys.stdout), pump(proc.stderr, "stderr", sys.stderr))
        code = await proc.wait()
        f.cancel()
        await flush()
        await post(f"exited {code} after {time.time() - start:.1f}s", level="error" if code else "info")
        if args.ask and reachable:
            question = (
                f"I ran `{' '.join(command)}` (exit code {code}). {args.ask}\n\nLast output:\n```\n"
                + "\n".join(tail[-150:])
                + "\n```"
            )
            r = await http.post(
                "/api/message", json={"text": question, "source": name, "wait": True}, timeout=None
            )
            print("\n" + r.json().get("reply", ""))
    return code


def cmd_status(args: argparse.Namespace) -> None:
    s = _server_call(args, "GET", "/api/status")
    if args.json:
        print(json.dumps(s, indent=1))
        return
    print(f"model      {s['model']}")
    print(
        f"context    ~{s['context_tokens']:,} / {s['context_window']:,} tokens (compacts at {s['compact_at']:,})"
    )
    print(
        f"thinking   {'on' if s['thinking'] else 'off'} for chat · {'busy' if s['busy'] else 'idle'} · queue {s['queue']}"
    )
    print(f"tools      {s['tools']}")
    for m in s["mcp"]:
        print(f"mcp        {m['name']}: {m['status']} ({m['tools']} tools) {m['error'] or ''}")
    print(f"waiting    {s['open_attention']} item(s) on you")


def cmd_routines(args: argparse.Namespace) -> None:
    if args.action == "run":
        if not args.name:
            _die("Usage: pa routines run NAME [input]")
        _server_call(args, "POST", f"/api/routines/{args.name}/run", json={"input": " ".join(args.input)})
        print(f"Started {args.name}. Watch it on the web page or in `pa chat`.")
        return
    if args.action == "reload":
        res = _server_call(args, "POST", "/api/routines/reload")
        print(f"Loaded {res['count']}." + (f" Errors: {res['errors']}" if res["errors"] else ""))
        return
    for r in _server_call(args, "GET", "/api/routines"):
        state = "" if r["enabled"] else " (disabled)"
        nxt = f" next {r['next_run'][:16].replace('T', ' ')}" if r["next_run"] else ""
        print(f"{r['kind']:8} {r['name']}{state}: {r['schedule'] or 'on demand'}{nxt}")
        if r["description"]:
            print(f"         {r['description']}")


def cmd_task(args: argparse.Namespace) -> None:
    res = _server_call(
        args,
        "POST",
        "/api/message",
        json={
            "text": f"/task {args.name} {' '.join(args.input)}".strip(),
            "source": args.source,
            "wait": True,
        },
        timeout=None,
    )
    print(res.get("reply", ""))


def cmd_tools(args: argparse.Namespace) -> None:
    for t in _server_call(args, "GET", "/api/tools"):
        print(f"{t['name']:48} {t['origin']:20} {t['description'][:70]}")


def cmd_wc(args: argparse.Namespace) -> None:
    print(_server_call(args, "GET", "/api/working-context")["content"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pa", description="Personal work agent")
    p.add_argument("--config", help="path to config.toml")
    p.add_argument("--url", help="agent URL for client commands (default from config, or $PA_URL)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create config files and the data folder").set_defaults(fn=cmd_init)
    c = sub.add_parser("check", help="test LM Studio, embeddings and MCP servers")
    c.add_argument("--skip-mcp", action="store_true")
    c.set_defaults(fn=cmd_check)

    s = sub.add_parser("serve", help="run the agent")
    s.add_argument("--no-scheduler", action="store_true", help="don't run routines on their schedules")
    s.add_argument("--no-mcp", action="store_true", help="don't start MCP servers")
    s.add_argument("--open", action="store_true", help="open the web page")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_serve)

    c = sub.add_parser("chat", help="terminal chat")
    c.add_argument("--source", default="terminal")
    c.add_argument("--reasoning", action="store_true", help="show the model's reasoning")
    c.set_defaults(fn=cmd_chat)

    a = sub.add_parser("ask", help="ask once and print the reply")
    a.add_argument("text", nargs="*")
    a.add_argument("--source", default="cli")
    a.add_argument("--think", action="store_true")
    a.add_argument("--fast", action="store_true")
    a.set_defaults(fn=cmd_ask)

    lg = sub.add_parser("log", help="post text to the feed")
    lg.add_argument("text", nargs="*")
    lg.add_argument("--source", default="script")
    lg.add_argument("--level", default="info", choices=["info", "warning", "error"])
    lg.set_defaults(fn=cmd_log)

    r = sub.add_parser("run", help="run a command and stream its output to the feed")
    r.add_argument("--name", help="label in the feed (default: the command name)")
    r.add_argument("--ask", help="when it finishes, ask the agent this about the output")
    r.add_argument("command", nargs=argparse.REMAINDER)
    r.set_defaults(fn=cmd_run)

    st = sub.add_parser("status", help="agent status")
    st.add_argument("--json", action="store_true")
    st.set_defaults(fn=cmd_status)

    ro = sub.add_parser("routines", help="list routines and tasks, or run one")
    ro.add_argument("action", nargs="?", choices=["list", "run", "reload"], default="list")
    ro.add_argument("name", nargs="?")
    ro.add_argument("input", nargs="*")
    ro.set_defaults(fn=cmd_routines)

    t = sub.add_parser("task", help="run a task")
    t.add_argument("name")
    t.add_argument("input", nargs="*")
    t.add_argument("--source", default="cli")
    t.set_defaults(fn=cmd_task)

    sub.add_parser("tools", help="list the agent's tools").set_defaults(fn=cmd_tools)
    sub.add_parser("wc", help="print the working context").set_defaults(fn=cmd_wc)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
