"""Backend relay for AskUserQuestion / sensitive-file permission card answers.

The chatdragon Responses pipe used to route the user's card click as a
regular user message with a hidden marker prefix.  That worked for the
happy path but had three real problems:

1. Open WebUI's auto-fired tasks (title generation, follow-up suggestions,
   tag generation) share the chat's ``chat_id`` and would race the user's
   click for the per-chat ``pending_function_call`` slot — sometimes the
   task's prompt got routed as the function_call_output, the gateway
   compared it against ``"allow"``, and silently denied.
2. Context injection (``<mlm_username>``, ``<dscrowd.token_key>``,
   thought_wrapped instructions) attached itself to the user's reply so
   the gateway saw ``"Allow\\n\\n<mlm_username>…"`` instead of just
   ``"Allow"``.
3. The pipe's ``self._pending_function_calls`` dict only worked under a
   single uvicorn worker — in a multi-worker deployment the click could
   land on a worker that didn't see the surface event and the answer
   would never reach the hook.

The fix is to skip the chat-completion channel entirely for card replies.
The frontend ``AskUserQuestionCard.svelte`` POSTs the user's choice to
``/api/v1/auq/answer`` here, and this endpoint relays it directly to the
gateway as ``function_call_output`` and streams the gateway's
continuation SSE back to the frontend.  No marker, no shared state,
no race.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from open_webui.env import (
    CLAUDE_CODE_GATEWAY_API_KEY,
    CLAUDE_CODE_GATEWAY_BASE_URL,
    CLAUDE_CODE_GATEWAY_TIMEOUT,
)
from open_webui.constants import ERROR_MESSAGES
from open_webui.models.chats import Chats
from open_webui.utils.auth import get_verified_user

log = logging.getLogger(__name__)

router = APIRouter()


class AUQAnswerRequest(BaseModel):
    """Body of POST /api/v1/auq/answer.

    All fields originate from the AskUserQuestion card the user just
    interacted with — the card holds them as render-time state from the
    pipe's ``<details type="ask_user_question" data-…>`` payload.
    """

    chat_id: str = Field(
        ...,
        description="Open WebUI chat that contains the card; used to verify ownership.",
    )
    call_id: str = Field(
        ...,
        description="The ``call_id`` of the function_call the gateway is waiting on.",
    )
    answer: str = Field(
        ...,
        description="The user's chosen option label or custom-text reply.",
    )
    previous_response_id: Optional[str] = Field(
        None,
        description=(
            "The gateway response id this card is associated with.  Required "
            "so the gateway can locate the paused session."
        ),
    )


def _build_gateway_headers(request: Request) -> dict:
    """Replicate the pipe's auth header construction so the gateway sees
    the same credentials regardless of which layer originated the call."""
    headers: dict[str, str] = {}
    if CLAUDE_CODE_GATEWAY_API_KEY:
        headers['Authorization'] = f'Bearer {CLAUDE_CODE_GATEWAY_API_KEY}'

    # Forward dscrowd cookie + Open WebUI username for MCP auth — same
    # contract as ``chatdragon_responses.pipe()``.
    cookie = (
        request.cookies.get('dscrowd.token_key')
        or request.headers.get('x-cookie-dscrowd.token_key')
        or ''
    )
    if cookie:
        headers['X-Cookie-dscrowd.token_key'] = cookie

    username = request.headers.get('x-openwebui-user-name', '')
    if username:
        try:
            username.encode('ascii')
            headers['X-OpenWebUI-User-Name'] = username
        except UnicodeEncodeError:
            from urllib.parse import quote

            headers['X-OpenWebUI-User-Name'] = quote(username)

    return headers


@router.post('/answer')
async def auq_answer(
    request: Request,
    body: AUQAnswerRequest,
    user=Depends(get_verified_user),
):
    """Relay an AskUserQuestion card reply to the gateway.

    Returns an SSE stream identical in shape to the gateway's own
    ``/v1/responses`` SSE so the frontend can pipe events into the same
    parser it uses for normal chat completions.
    """
    # Reject if the caller doesn't own the chat (unless admin) — this
    # mirrors the chat-completion-side ownership gate.
    if (
        not body.chat_id.startswith('local:')
        and not await Chats.is_chat_owner(body.chat_id, user.id)
        and user.role != 'admin'
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=ERROR_MESSAGES.DEFAULT(),
        )

    payload: dict = {
        'input': [
            {
                'type': 'function_call_output',
                'call_id': body.call_id,
                'output': body.answer,
            }
        ],
        'stream': True,
    }
    if body.previous_response_id:
        payload['previous_response_id'] = body.previous_response_id

    # Forward the resolved Open WebUI username as the wrapper-side
    # ``user`` so workspace isolation matches the pipe's behaviour.
    if user and user.name:
        payload['user'] = user.name

    url = f'{CLAUDE_CODE_GATEWAY_BASE_URL}/v1/responses'
    headers = _build_gateway_headers(request)

    log.info(
        '[AUQ-ANSWER] relay chat=%s call_id=%s prev=%s user=%s',
        body.chat_id,
        body.call_id,
        body.previous_response_id,
        user.name,
    )

    async def _stream():
        timeout = httpx.Timeout(
            connect=30.0,
            read=float(CLAUDE_CODE_GATEWAY_TIMEOUT),
            write=30.0,
            pool=30.0,
        )
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream(
                    'POST', url, json=payload, headers=headers
                ) as resp:
                    if resp.status_code != 200:
                        body_text = (await resp.aread()).decode('utf-8', 'replace')
                        log.error(
                            '[AUQ-ANSWER] gateway %s: %s',
                            resp.status_code,
                            body_text[:500],
                        )
                        # Emit a single SSE error so the frontend stream
                        # parser can surface it without dying mid-pipe.
                        yield (
                            f'event: response.failed\n'
                            f'data: {json.dumps({"type": "response.failed", "error": {"code": resp.status_code, "message": body_text[:500]}})}\n\n'
                        )
                        return

                    async for raw in resp.aiter_raw():
                        if raw:
                            yield raw
        except Exception as exc:  # pragma: no cover - network surface
            log.exception('[AUQ-ANSWER] relay failed')
            yield (
                f'event: response.failed\n'
                f'data: {json.dumps({"type": "response.failed", "error": {"code": "relay_error", "message": str(exc)}})}\n\n'
            )

    return StreamingResponse(_stream(), media_type='text/event-stream')
