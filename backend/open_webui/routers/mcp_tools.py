"""API endpoint to list available MCP tools from the gateway config.

Reads the mounted mcp-config.json file and returns a simplified
tool list for the frontend dropdown.
"""

import json
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

router = APIRouter()

MCP_CONFIG_PATH = os.getenv("MCP_CONFIG_PATH", "/app/mcp-config.json")


def _load_mcp_tools() -> list[dict]:
    """Parse mcp-config.json and return tool list with id and display name."""
    config_path = Path(MCP_CONFIG_PATH)
    if not config_path.is_file():
        return []

    try:
        with open(config_path) as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("Failed to read MCP config: %s", e)
        return []

    if not isinstance(raw, dict):
        return []

    servers = raw.get("mcpServers", raw)
    tools = []
    for name, config in servers.items():
        # Tool pattern: mcp__server_name__* (dashes replaced with underscores)
        safe_name = "_".join(name.split("-"))
        pattern = f"mcp__{safe_name}__*"
        # Display name: use config's description or clean up the server name
        display = config.get("description", "")
        if not display:
            display = name.replace("-", " ").replace("_", " ").title()
        tools.append({
            "id": pattern,
            "name": display,
            "server": name,
        })

    return tools


@router.get("/mcp_tools")
async def get_mcp_tools():
    """Return available MCP tools for the frontend dropdown."""
    tools = _load_mcp_tools()
    return JSONResponse(
        content=tools,
        headers={"Cache-Control": "public, max-age=60"},
    )
