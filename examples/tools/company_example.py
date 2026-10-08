"""Example custom tools. Copy into tools/ (and edit) to give the agent new abilities.

Any function marked @tool in tools/*.py becomes a tool. The parameters come from the type
hints, and the description comes from the docstring. Functions can be sync or async. A
parameter named `ctx` receives the ToolContext (ctx.app is the running agent).
"""

from __future__ import annotations

import os
import subprocess

import httpx

from personal_agentic import tool


@tool(policy="allow", read_only=True)
async def company_wiki_search(query: str, limit: int = 5) -> str:
    """Search the internal wiki and return matching page titles and links.

    Args:
        query: What to search for.
        limit: How many results to return.
    """
    base = os.environ.get("WIKI_URL", "https://wiki.example.internal")
    async with httpx.AsyncClient(
        timeout=20, headers={"Authorization": f"Bearer {os.environ.get('WIKI_TOKEN', '')}"}
    ) as http:
        r = await http.get(f"{base}/api/search", params={"q": query, "limit": limit})
        r.raise_for_status()
        return "\n".join(f"- {hit['title']}: {hit['url']}" for hit in r.json().get("results", []))


@tool(policy="ask")
def run_deploy_check(service: str) -> str:
    """Run the deploy readiness script for a service (asks before running).

    Args:
        service: Service name as used by the script.
    """
    out = subprocess.run(["./scripts/deploy_check.sh", service], capture_output=True, text=True, timeout=300)
    return f"exit {out.returncode}\n{out.stdout[-8000:]}\n{out.stderr[-2000:]}"
