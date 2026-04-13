"""Proxy routes for an external image server.

Provides ``/get_image_list`` and ``/get_image`` endpoints that forward
requests to the upstream image server identified by the
``IMAGE_SERVER_BASE`` environment variable.  The proxy adds internal
authentication headers and adjusts the folder prefixes in list responses
so the frontend receives fully-qualified paths.
"""

import json
import logging
import os

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse

log = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

IMAGE_SERVER_BASE: str = os.getenv("IMAGE_SERVER_BASE", "")
IMAGE_INTERNAL_SECRET: str = os.getenv("IMAGE_INTERNAL_SECRET", "")
IMAGE_TLS_VERIFY: bool = os.getenv("IMAGE_TLS_VERIFY", "true").lower() in (
    "true",
    "1",
    "yes",
)

PASS_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "cache-control",
        "etag",
        "last-modified",
    }
)


def _require_image_server():
    if not IMAGE_SERVER_BASE:
        raise HTTPException(503, "IMAGE_SERVER_BASE is not configured")


# ---------------------------------------------------------------------------
# GET /get_image_list
# ---------------------------------------------------------------------------


@router.get("/get_image_list")
async def get_image_list(
    filename: str = Query(
        ...,
        description="Path of an image; its parent directory is used to list siblings",
    ),
    background_tasks: BackgroundTasks = None,
):
    """Return the list of images that live in the same folder as *filename*."""

    _require_image_server()
    upstream_url = f"{IMAGE_SERVER_BASE.rstrip('/')}/api/get_image_list"
    folder = os.path.dirname(filename)
    params = {"folder": folder}
    headers = {
        "X-From-Chat": "true",
        "x-internal-secret": IMAGE_INTERNAL_SECRET,
    }

    client = httpx.AsyncClient(
        verify=IMAGE_TLS_VERIFY,
        timeout=30.0,
        follow_redirects=True,
    )
    try:
        req = client.build_request("GET", upstream_url, params=params, headers=headers)
        r = await client.send(req, stream=True)

        if r.status_code != 200:
            body = await r.aread()
            await r.aclose()
            await client.aclose()
            return Response(
                content=body,
                status_code=r.status_code,
                media_type=r.headers.get("content-type", "text/plain"),
                headers={"Cross-Origin-Resource-Policy": "cross-origin"},
            )

        content_type = r.headers.get("content-type", "")

        # JSON response - prefix every filename with the folder path
        if "application/json" in content_type or content_type.endswith("+json"):
            body = await r.aread()
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                data = body

            def _prefix(x: str) -> str:
                return f"{folder.rstrip('/')}/{str(x).lstrip('/')}"

            if isinstance(data, list):
                prefixed = [_prefix(x) for x in data]
            elif isinstance(data, dict):
                for key in ("images", "data", "items"):
                    if isinstance(data.get(key), list):
                        data[key] = [_prefix(x) for x in data[key]]
                prefixed = data
            else:
                prefixed = data

            await r.aclose()
            await client.aclose()
            return JSONResponse(
                content=prefixed,
                headers={"Cross-Origin-Resource-Policy": "cross-origin"},
            )

        # Non-JSON - stream through unchanged
        media_type = r.headers.get("content-type", "application/octet-stream")
        out_headers = {
            k: v for k, v in r.headers.items() if k.lower() in PASS_HEADERS
        }
        out_headers["Cross-Origin-Resource-Policy"] = "cross-origin"

        async def iterator():
            try:
                async for chunk in r.aiter_bytes():
                    yield chunk
            finally:
                if background_tasks is None:
                    await r.aclose()
                    await client.aclose()

        if background_tasks is not None:
            background_tasks.add_task(r.aclose)
            background_tasks.add_task(client.aclose)

        return StreamingResponse(
            iterator(), media_type=media_type, headers=out_headers
        )

    except httpx.HTTPError as e:
        try:
            await client.aclose()
        except Exception:
            pass
        raise HTTPException(502, f"Upstream connection error: {e}")


# ---------------------------------------------------------------------------
# GET /get_image
# ---------------------------------------------------------------------------


@router.get("/get_image")
async def get_image(
    filename: str = Query(..., description="Image filename"),
    folder: str = Query("", description="Folder path"),
    background_tasks: BackgroundTasks = None,
):
    """Return binary image data for a single file from the image server."""

    _require_image_server()
    upstream_url = f"{IMAGE_SERVER_BASE.rstrip('/')}/api/get_image"

    if folder and not folder.startswith("/"):
        folder = "/" + folder
    full_path = os.path.join(folder, filename) if folder else filename

    params = {"filename": full_path, "folder": ""}
    headers = {
        "X-From-Chat": "true",
        "x-internal-secret": IMAGE_INTERNAL_SECRET,
    }

    client = httpx.AsyncClient(
        verify=IMAGE_TLS_VERIFY,
        timeout=30.0,
        follow_redirects=True,
    )
    try:
        req = client.build_request("GET", upstream_url, params=params, headers=headers)
        r = await client.send(req, stream=True)

        if r.status_code != 200:
            body = await r.aread()
            await r.aclose()
            await client.aclose()
            return Response(
                content=body,
                status_code=r.status_code,
                media_type=r.headers.get("content-type", "text/plain"),
                headers={"Cross-Origin-Resource-Policy": "cross-origin"},
            )

        media_type = r.headers.get("content-type", "application/octet-stream")
        out_headers = {
            k: v for k, v in r.headers.items() if k.lower() in PASS_HEADERS
        }
        out_headers["Cross-Origin-Resource-Policy"] = "cross-origin"

        async def iterator():
            async for chunk in r.aiter_bytes():
                yield chunk

        if background_tasks is not None:
            background_tasks.add_task(r.aclose)
            background_tasks.add_task(client.aclose)

        return StreamingResponse(
            iterator(), media_type=media_type, headers=out_headers
        )

    except httpx.HTTPError as e:
        try:
            await client.aclose()
        except Exception:
            pass
        raise HTTPException(502, f"Upstream connection error: {e}")


# ---------------------------------------------------------------------------
# GET /fetch?url=<http-url>
# ---------------------------------------------------------------------------


@router.get("/fetch")
async def fetch_image(
    url: str = Query(..., description="HTTP/HTTPS image URL to proxy"),
    background_tasks: BackgroundTasks = None,
):
    """Generic image proxy that fetches any HTTP/HTTPS URL and streams it back.

    Used to work around Mixed Content errors when HTTPS pages try to load
    HTTP images.
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(400, "url must start with http:// or https://")

    client = httpx.AsyncClient(
        verify=False,
        timeout=30.0,
        follow_redirects=True,
    )
    try:
        req = client.build_request("GET", url)
        r = await client.send(req, stream=True)

        if r.status_code != 200:
            body = await r.aread()
            await r.aclose()
            await client.aclose()
            return Response(
                content=body,
                status_code=r.status_code,
                media_type=r.headers.get("content-type", "text/plain"),
            )

        media_type = r.headers.get("content-type", "application/octet-stream")
        out_headers = {
            k: v for k, v in r.headers.items() if k.lower() in PASS_HEADERS
        }
        out_headers["Cross-Origin-Resource-Policy"] = "cross-origin"
        out_headers["Cache-Control"] = "public, max-age=3600"

        async def iterator():
            async for chunk in r.aiter_bytes():
                yield chunk

        if background_tasks is not None:
            background_tasks.add_task(r.aclose)
            background_tasks.add_task(client.aclose)

        return StreamingResponse(
            iterator(), media_type=media_type, headers=out_headers
        )

    except httpx.HTTPError as e:
        try:
            await client.aclose()
        except Exception:
            pass
        raise HTTPException(502, f"Upstream connection error: {e}")
