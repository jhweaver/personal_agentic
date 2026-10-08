from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_lmstudio import make_app  # noqa: E402

from personal_agentic.config import Config  # noqa: E402


class _Server(uvicorn.Server):
    def install_signal_handlers(self) -> None:  # running in a thread
        pass


@pytest.fixture(scope="session")
def fake_lm():
    app = make_app()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = _Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}/v1", "app": app}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def cfg(tmp_path: Path, fake_lm) -> Config:
    c = Config.model_validate(
        {
            "llm": {"base_url": fake_lm["url"]},
            "paths": {
                "data_dir": str(tmp_path / "data"),
                "working_context": str(tmp_path / "data" / "wc.md"),
                "routines_dir": str(tmp_path / "routines"),
                "tasks_dir": str(tmp_path / "tasks"),
                "tools_dir": str(tmp_path / "tools"),
                "mcp_config": str(tmp_path / "mcp.json"),
                "system_prompt": str(tmp_path / "prompts" / "system.md"),
            },
            "notify": {"imessage_self": "+15555550100"},
            "workday": {
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "start": "00:00",
                "end": "23:59",
            },
        }
    )
    c.base_dir = tmp_path
    for d in ("routines", "tasks", "tools"):
        (tmp_path / d).mkdir()
    return c


@pytest.fixture
async def agent(cfg):
    from personal_agentic.app import AgentApp

    a = AgentApp(cfg)
    await a.start(scheduler=False, mcp=False)
    yield a
    await a.stop()


def _serve_in_thread(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = _Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    return port, server, thread


@pytest.fixture(scope="session")
def _oauth_server_session():
    from fake_oauth_server import make_app as make_oauth_app

    app, state = make_oauth_app()
    port, server, thread = _serve_in_thread(app)
    state.base = f"http://127.0.0.1:{port}"
    yield {"url": f"http://127.0.0.1:{port}/mcp", "state": state}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def oauth_server(_oauth_server_session):
    st = _oauth_server_session["state"]
    st.dcr, st.expires_in, st.refresh_count = True, 3600, 0
    st.token_requests.clear()
    st.authorize_requests.clear()
    return _oauth_server_session


@pytest.fixture
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port
