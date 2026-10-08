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
