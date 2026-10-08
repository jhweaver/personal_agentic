"""An MCP server behind a minimal OAuth 2.1 authorization server, for testing sign-in.

Supports discovery (RFC 9728 / 8414), dynamic client registration (switchable), one
pre-registered confidential client, PKCE, refresh tokens and short-lived access tokens.
The authorize endpoint approves immediately and redirects back, like a user clicking Allow.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

STATIC_ID = "static-client"
STATIC_SECRET = "static-secret"


class OAuthState:
    def __init__(self) -> None:
        self.base = ""
        self.dcr = True
        self.expires_in = 3600
        self.clients: dict[str, dict[str, Any]] = {STATIC_ID: {"secret": STATIC_SECRET}}
        self.codes: dict[str, dict[str, Any]] = {}
        self.access: dict[str, float] = {}
        self.refresh: dict[str, str] = {}
        self.refresh_count = 0
        self.token_requests: list[dict[str, str]] = []
        self.authorize_requests: list[dict[str, str]] = []


def make_app() -> tuple[Any, OAuthState]:
    mcp = MCPServer("protected")

    @mcp.tool()
    def whoami() -> str:
        """Who is signed in."""
        return "signed-in user"

    @mcp.tool()
    def search_docs(query: str) -> str:
        """Search docs."""
        return f"3 docs about {query}"

    inner = mcp.streamable_http_app(host="127.0.0.1")
    state = OAuthState()

    def issue(client_id: str) -> dict[str, Any]:
        access, refresh = secrets.token_hex(8), secrets.token_hex(8)
        state.access[access] = time.time() + state.expires_in
        state.refresh[refresh] = client_id
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": state.expires_in,
            "refresh_token": refresh,
        }

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        req = Request(scope, receive)
        base = state.base or f"http://{req.headers['host']}"
        path = scope["path"]
        resp: Response | None = None

        if path.startswith("/.well-known/oauth-protected-resource"):
            resp = JSONResponse({"resource": f"{base}/mcp", "authorization_servers": [base]})
        elif path.startswith("/.well-known/oauth-authorization-server") or path.startswith(
            "/.well-known/openid-configuration"
        ):
            meta = {
                "issuer": base,
                "authorization_endpoint": f"{base}/authorize",
                "token_endpoint": f"{base}/token",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "code_challenge_methods_supported": ["S256"],
                "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
            }
            if state.dcr:
                meta["registration_endpoint"] = f"{base}/register"
            resp = JSONResponse(meta)
        elif path == "/register":
            if not state.dcr:
                resp = JSONResponse({"error": "registration not supported"}, status_code=404)
            else:
                body = await req.json()
                cid = f"dyn-{secrets.token_hex(4)}"
                state.clients[cid] = {"secret": None, "redirect_uris": body.get("redirect_uris")}
                resp = JSONResponse(
                    {**body, "client_id": cid, "token_endpoint_auth_method": "none"}, status_code=201
                )
        elif path == "/authorize":
            q = dict(req.query_params)
            state.authorize_requests.append(q)
            if q.get("client_id") not in state.clients or q.get("code_challenge_method") != "S256":
                resp = JSONResponse({"error": "invalid_request"}, status_code=400)
            else:
                code = secrets.token_hex(8)
                state.codes[code] = {
                    "client_id": q["client_id"],
                    "challenge": q["code_challenge"],
                    "redirect_uri": q["redirect_uri"],
                }
                params = {"code": code, "state": q.get("state", "")}
                resp = RedirectResponse(f"{q['redirect_uri']}?{urlencode(params)}", status_code=302)
        elif path == "/token":
            form = dict(await req.form())
            state.token_requests.append(form)
            client = state.clients.get(form.get("client_id", ""))
            if client is None or (client["secret"] and form.get("client_secret") != client["secret"]):
                resp = JSONResponse({"error": "invalid_client"}, status_code=401)
            elif form.get("grant_type") == "authorization_code":
                code = state.codes.pop(form.get("code", ""), None)
                verifier = form.get("code_verifier", "")
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                )
                if (
                    not code
                    or code["challenge"] != challenge
                    or code["redirect_uri"] != form.get("redirect_uri")
                ):
                    resp = JSONResponse({"error": "invalid_grant"}, status_code=400)
                else:
                    resp = JSONResponse(issue(form["client_id"]))
            elif form.get("grant_type") == "refresh_token":
                owner = state.refresh.pop(form.get("refresh_token", ""), None)
                if owner is None:
                    resp = JSONResponse({"error": "invalid_grant"}, status_code=400)
                else:
                    state.refresh_count += 1
                    resp = JSONResponse(issue(owner))
            else:
                resp = JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        elif path.startswith("/mcp"):
            auth = req.headers.get("authorization", "")
            token = auth[7:] if auth.lower().startswith("bearer ") else ""
            if state.access.get(token, 0) < time.time():
                resp = Response(
                    status_code=401,
                    headers={
                        "WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
                    },
                )
        if resp is not None:
            return await resp(scope, receive, send)
        return await inner(scope, receive, send)

    return app, state
