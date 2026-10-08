from __future__ import annotations

import time

from fastapi.testclient import TestClient

from personal_agentic.server.api import create_app


def wait_for(fn, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_api_and_websocket_mirror(cfg):
    app = create_app(cfg, scheduler=False, mcp=False)
    headers = {"host": "127.0.0.1:8765"}
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        # a terminal client and the web page both watch the feed
        with client.websocket_connect("/ws?client=web", headers=headers) as web:
            hello = web.receive_json()
            assert hello["type"] == "hello" and hello["status"]["model"] == cfg.llm.model

            # a script asks something over HTTP and waits for the answer
            r = client.post(
                "/api/message", json={"text": "status of the build?", "source": "nightly", "wait": True}
            )
            assert r.json() == {"reply": "Echo: status of the build?"}

            seen = []
            while not any(e["kind"] == "assistant_message" for e in seen):
                msg = web.receive_json()
                if msg["type"] == "event":
                    seen.append(msg["event"])
            user = next(e for e in seen if e["kind"] == "user_message")
            assert user["source"] == "nightly"

            # messages typed on the web page go through the websocket
            web.send_json({"type": "message", "text": "from the web", "source": "web"})
            while True:
                msg = web.receive_json()
                if msg["type"] == "event" and msg["event"]["kind"] == "assistant_message":
                    assert msg["event"]["data"]["text"] == "Echo: from the web"
                    break

            # script output lands in the feed
            client.post("/api/log", json={"source": "deploy.sh", "text": "step 1 ok\nstep 2 ok"})
            while True:
                msg = web.receive_json()
                if msg["type"] == "event" and msg["event"]["kind"] == "script_output":
                    assert msg["event"]["source"] == "deploy.sh"
                    break

        events = client.get("/api/events", params={"session": "main"}).json()
        assert [e["kind"] for e in events].count("assistant_message") == 2
        st = client.get("/api/status").json()
        assert st["context_window"] == 131072 and st["tools"] > 10

        wc = client.get("/api/working-context").json()
        r = client.put("/api/working-context", json={"content": wc["content"] + "\n- edited on the web\n"})
        assert r.status_code == 200
        assert "edited on the web" in client.get("/api/working-context").json()["content"]

        assert client.get("/api/routines").json() == []
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200


def test_rejects_cross_origin_and_bad_host(cfg):
    app = create_app(cfg, scheduler=False, mcp=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        r = client.post("/api/message", json={"text": "x"}, headers={"origin": "https://evil.example"})
        assert r.status_code == 403
        r = client.get("/api/status", headers={"host": "evil.example"})
        assert r.status_code == 403
        r = client.post(
            "/api/log", json={"source": "s", "text": "ok"}, headers={"origin": "http://127.0.0.1:8765"}
        )
        assert r.status_code == 200
