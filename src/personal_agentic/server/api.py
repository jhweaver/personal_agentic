"""Local HTTP + WebSocket server: the web page, the terminal client and scripts all use it.

Only listens on 127.0.0.1. Requests must carry a localhost Host header, and browser
requests must come from this page's own origin, so other websites you visit can't drive
the agent (DNS rebinding / cross-site requests).
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..app import AgentApp
from ..config import Config

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class MessageIn(BaseModel):
    text: str
    source: str = "api"
    wait: bool = False
    thinking: bool | None = None


class LogIn(BaseModel):
    source: str
    text: str
    level: str = "info"
    stream: str | None = None


class ApprovalIn(BaseModel):
    approved: bool
    note: str = ""


class WorkingContextIn(BaseModel):
    content: str


class RunIn(BaseModel):
    input: str = ""


def _host_ok(host_header: str | None) -> bool:
    if not host_header:
        return False
    host = (
        host_header.rsplit(":", 1)[0] if not host_header.startswith("[") else host_header.split("]")[0] + "]"
    )
    return host in LOCAL_HOSTS


def _origin_ok(origin: str | None, cfg: Config) -> bool:
    if origin is None:
        return True  # not a browser (curl, pa, scripts)
    port = cfg.server.port
    return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}


def create_app(
    cfg: Config, agent: AgentApp | None = None, *, scheduler: bool = True, mcp: bool = True
) -> FastAPI:
    holder: dict[str, AgentApp] = {}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        a = agent or AgentApp(cfg)
        holder["agent"] = a
        await a.start(scheduler=scheduler, mcp=mcp)
        try:
            yield
        finally:
            await a.stop()

    api = FastAPI(title="personal-agentic", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)

    def A() -> AgentApp:
        return holder["agent"]

    @api.middleware("http")
    async def local_only(request: Request, call_next):
        if not _host_ok(request.headers.get("host")):
            return JSONResponse({"error": "bad host"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and not _origin_ok(
            request.headers.get("origin"), cfg
        ):
            return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
        return await call_next(request)

    # ---- page ----------------------------------------------------------------------------------------
    @api.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})

    api.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---- messages & feed --------------------------------------------------------------------------------
    @api.post("/api/message")
    async def message(body: MessageIn) -> dict[str, Any]:
        fut = A().submit(body.text, source=body.source, thinking=body.thinking)
        if not body.wait:
            return {"queued": True}
        reply = await fut
        return {"reply": reply}

    @api.post("/api/log")
    async def post_log(body: LogIn) -> dict[str, Any]:
        A().bus.emit(
            "script_output",
            {"text": body.text[-20000:], "level": body.level, "stream": body.stream},
            session="main",
            source=body.source,
        )
        return {"ok": True}

    @api.get("/api/events")
    async def events(
        session: str | None = "main", after: int = 0, before: int | None = None, limit: int = 300
    ):
        return A().store.events(
            session=session or None, after_id=after, before_id=before, limit=min(limit, 2000)
        )

    @api.post("/api/stop")
    async def stop() -> dict[str, Any]:
        return {"stopped": A().stop_current()}

    @api.get("/api/status")
    async def status() -> dict[str, Any]:
        return await A().status()

    # ---- attention --------------------------------------------------------------------------------------
    @api.get("/api/attention")
    async def attention() -> list[dict[str, Any]]:
        return A().store.open_attention()

    @api.post("/api/attention/{aid}")
    async def resolve(aid: int, body: ApprovalIn) -> dict[str, Any]:
        if not A().attention.resolve(aid, body.approved, body.note, via="api"):
            raise HTTPException(404, "No open item with that id")
        return {"ok": True}

    # ---- sessions (sub-agents, routine runs) --------------------------------------------------------------
    @api.get("/api/sessions")
    async def sessions(limit: int = 50) -> list[dict[str, Any]]:
        return [s for s in A().store.list_sessions(limit=limit) if s["kind"] != "main"]

    @api.post("/api/sessions/{sid}/cancel")
    async def cancel(sid: str) -> dict[str, Any]:
        return {"cancelled": A().cancel_subagent(sid)}

    # ---- working context -----------------------------------------------------------------------------------
    @api.get("/api/working-context")
    async def wc_get() -> dict[str, Any]:
        return {"content": A().wc.read(), "path": str(A().wc.path), "versions": A().store.wc_versions()}

    @api.put("/api/working-context")
    async def wc_put(body: WorkingContextIn) -> dict[str, Any]:
        A().wc.write(body.content, author="user:web")
        A().bus.emit("working_context_changed", {"author": "user:web"}, source="web")
        return {"ok": True}

    # ---- routines & tasks ----------------------------------------------------------------------------------
    @api.get("/api/routines")
    async def routines() -> list[dict[str, Any]]:
        return A().routines.listing()

    @api.post("/api/routines/{name}/run")
    async def run_routine(name: str, body: RunIn | None = None) -> dict[str, Any]:
        d = A().routines.defs.get(name)
        if not d:
            raise HTTPException(404, f"No routine or task named {name!r}")
        A().routines.trigger(name, trigger="web", input_text=body.input if body else "")
        return {"started": True}

    @api.post("/api/routines/reload")
    async def reload_routines() -> dict[str, Any]:
        A().routines.load()
        return {"count": len(A().routines.defs), "errors": A().routines.errors}

    @api.post("/api/mcp/reload")
    async def reload_mcp() -> list[dict[str, Any]]:
        await A().mcp.reload()
        return A().mcp.status()

    @api.get("/api/tools")
    async def tools() -> list[dict[str, Any]]:
        return [
            {"name": t.name, "origin": t.origin, "description": t.description[:300], "read_only": t.read_only}
            for t in A().tools.all()
        ]

    # ---- websocket feed ---------------------------------------------------------------------------------------
    @api.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        if not _host_ok(socket.headers.get("host")) or not _origin_ok(socket.headers.get("origin"), cfg):
            await socket.close(code=4403)
            return
        await socket.accept()
        agent_app = A()
        queue = agent_app.bus.subscribe()
        client = socket.query_params.get("client", "web")
        try:
            await socket.send_json({"type": "hello", "status": await agent_app.status()})

            async def pump() -> None:
                while True:
                    event = await queue.get()
                    await socket.send_text(json.dumps({"type": "event", "event": event}, default=str))

            pump_task = asyncio.create_task(pump())
            try:
                while True:
                    msg = await socket.receive_json()
                    await _handle_ws(agent_app, msg, client, socket)
            finally:
                pump_task.cancel()
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("websocket error")
        finally:
            agent_app.bus.unsubscribe(queue)

    return api


async def _handle_ws(agent: AgentApp, msg: dict[str, Any], client: str, socket: WebSocket) -> None:
    kind = msg.get("type")
    if kind == "message":
        text = str(msg.get("text", "")).strip()
        if text:
            agent.submit(text, source=msg.get("source") or client, thinking=msg.get("thinking"))
    elif kind == "resolve":
        ok = agent.attention.resolve(
            int(msg["id"]), bool(msg.get("approved", True)), msg.get("note", ""), via=client
        )
        if not ok:
            await socket.send_json({"type": "error", "text": f"No open item #{msg['id']}"})
    elif kind == "stop":
        agent.stop_current()
    elif kind == "cancel":
        agent.cancel_subagent(str(msg.get("id")))
    elif kind == "ping":
        await socket.send_json({"type": "pong"})
