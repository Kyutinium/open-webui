"""Confluence authentication helper.

Checks for the dscrowd.token_key cookie (set by Confluence SSO on
the .gwanghands.net domain) and returns the token value so the
frontend can pass it to the pipe.
"""

import logging
import os

from fastapi import APIRouter, Cookie, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)

router = APIRouter()

CONFLUENCE_BASE_URL = os.getenv(
    "CONFLUENCE_BASE_URL", "https://confluence.gwanghands.net"
)


@router.get("/confluence/check")
async def check_confluence_token(
    request: Request,
):
    """Check if dscrowd.token_key cookie exists and return status."""
    token = request.cookies.get("dscrowd.token_key", "")
    if token:
        return JSONResponse(content={
            "authenticated": True,
            "token": token,
        })
    else:
        login_url = f"{CONFLUENCE_BASE_URL}/login.action"
        return JSONResponse(content={
            "authenticated": False,
            "login_url": login_url,
        })
