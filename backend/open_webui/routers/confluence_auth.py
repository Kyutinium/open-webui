"""Confluence authentication helper.

Checks for the dscrowd.token_key cookie (set by Confluence SSO on
the .gwanghands.net domain) and returns the token value so the
frontend can pass it to the pipe.
"""

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger(__name__)

router = APIRouter()

CONFLUENCE_BASE_URL = os.getenv(
    "CONFLUENCE_BASE_URL", "https://confluence.gwanghands.net"
)
WEBUI_URL = os.getenv("WEBUI_URL", "")


@router.get("/confluence/check")
async def check_confluence_token(request: Request):
    """Check if dscrowd.token_key cookie exists and return status."""
    token = request.cookies.get("dscrowd.token_key", "")
    base_url = WEBUI_URL or str(request.base_url).rstrip("/")
    redirect_dest = f"{base_url}/api/v1/confluence/login-success"
    login_url = f"{CONFLUENCE_BASE_URL}/login.action?os_destination={redirect_dest}"
    if token:
        return JSONResponse(content={
            "authenticated": True,
        })
    else:
        return JSONResponse(content={
            "authenticated": False,
            "login_url": login_url,
        })


@router.get("/confluence/login-success")
async def confluence_login_success():
    """Landing page after Confluence login. Auto-closes the popup."""
    return HTMLResponse(content="""
<!DOCTYPE html>
<html>
<head><title>Login Success</title></head>
<body>
<p>Confluence login successful. This window will close automatically.</p>
<script>
    window.close();
    // Fallback if window.close() is blocked
    setTimeout(function() {
        document.body.innerHTML = '<p>Login successful. You can close this window.</p>';
    }, 1000);
</script>
</body>
</html>
""")
