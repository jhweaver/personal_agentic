"""Talk to the running agent from your own Python scripts.

from personal_agentic.client import AgentClient

agent = AgentClient(source="nightly-report")
agent.log("starting export")                         # shows on the web page
answer = agent.ask("Summarize these failures: ...")  # waits for the reply
"""

from __future__ import annotations

import os
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8765"


def server_url(explicit: str | None = None) -> str:
    if explicit:
        return explicit.rstrip("/")
    if os.environ.get("PA_URL"):
        return os.environ["PA_URL"].rstrip("/")
    try:
        from .config import load_config

        return load_config().server_url
    except Exception:
        return DEFAULT_URL


class AgentClient:
    def __init__(self, url: str | None = None, source: str = "script", timeout: float | None = None):
        self.url = server_url(url)
        self.source = source
        self._http = httpx.Client(base_url=self.url, timeout=timeout)

    def ask(self, text: str, *, wait: bool = True, thinking: bool | None = None) -> str | None:
        r = self._http.post(
            "/api/message", json={"text": text, "source": self.source, "wait": wait, "thinking": thinking}
        )
        r.raise_for_status()
        return r.json().get("reply") if wait else None

    def log(self, text: str, *, level: str = "info", stream: str | None = None) -> None:
        r = self._http.post(
            "/api/log", json={"text": text, "source": self.source, "level": level, "stream": stream}
        )
        r.raise_for_status()

    def status(self) -> dict[str, Any]:
        r = self._http.get("/api/status")
        r.raise_for_status()
        return r.json()

    def run_routine(self, name: str, input_text: str = "") -> None:
        r = self._http.post(f"/api/routines/{name}/run", json={"input": input_text})
        r.raise_for_status()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> AgentClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
