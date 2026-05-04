"""
title: Chatdragon Responses (OpenCode)
author: oh-my-gateway
version: 0.2.0
description:
    OpenCode-aware pipe for the gateway, with strict ``<think>`` /
    ``<details>`` tag isolation.  Supersedes
    ``chatdragon_responses_opencode_slim``.

    Why a new pipe?  The slim pipe tracked ``<think>`` state with a
    naive ``"<think>" in chunk`` substring scan that desynced when
    tags split across deltas, when the model emitted both tags in
    one chunk, or when literal ``<think>`` appeared in a code fence.
    A desynced flag let tool ``<details>`` blocks land *inside* an
    open ``<think>`` (Open WebUI then nested the tool collapsible
    inside the collapsed thought) and let stale ``</think>`` strings
    leak through, wrapping non-thinking content in a ``<think>``
    that never opened locally.

    What changed:

    - **Streaming tag SCANNER** with a 7-char hold-back buffer,
      replacing the substring scan.  Tags that split across chunks
      (``"<thi"`` + ``"nk>"``) are reassembled correctly.
    - **Strict block STATE MACHINE** — at most one block (``<think>``
      OR a tool ``<details>``) is open at any moment.  Tool blocks
      are atomic: emitted as a self-contained ``<details>...</details>``
      after a forced ``goto(IDLE)`` that closes any open think.
    - **Pipe owns canonical ``<think>`` emission.**  Raw upstream
      ``<think>``/``</think>`` are stripped from text deltas; the
      pipe re-emits its own canonical pairs.  No more leaked stale
      tags, no more open/close balance counters.
    - **MCP tool-name detection handles both conventions**:
      ``mcp__<server>__<tool>`` (Claude SDK) AND ``<server>:<tool>``
      (opencode native — see ``mcp/index.ts`` ``sanitize`` logic in
      the opencode repo).
    - **Final-flush blocks** (``search_results_button``,
      ``image_gallery``, ``tool_explorer``) emit AFTER ``goto(IDLE)``
      so they can never land inside an open ``<think>`` or another
      ``<details>``.

    Preserved from the slim pipe: session continuity via
    ``previous_response_id``, 409 stale-id recovery, user context
    injection, MCP credential forwarding, image upload → shared
    volume, tool explorer / image gallery rendering.

    Pipe id: ``chatdragon-responses-opencode``

    This file imports parsing / rendering / MCP-detection helpers
    from ``chatdragon_responses_opencode_helpers`` (a sibling
    module that lives in the same directory).  Both files must be
    deployed together.
license: MIT
"""

import base64
import html
import json
import logging
import random
import re
import threading
from pathlib import Path
from typing import Iterator, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

import httpx

# Helpers live in a sibling module; see the docstring.
from chatdragon_responses_opencode_helpers import (
    STALE_RESP_ID_RE as _STALE_RESP_ID_RE,
    safe_attr as _safe_attr,
    is_mcp_tool,
    mcp_label_key,
    parse_tool_content,
    extract_thumbnails_from_tool_result,
    extract_tool_results_for_explorer,
    extract_tool_result_text,
    build_gallery_tag,
    build_tool_explorer_tag,
    tool_label,
    friendly_tool_notification,
)


log = logging.getLogger(__name__)


class Pipeline:
    class Valves(BaseModel):
        BASE_URL: str = Field(
            default="http://host.docker.internal:17995",
            description="Claude Code Gateway server URL",
        )
        API_KEY: str = Field(
            default="",
            description="API key for the gateway server (leave empty if not required)",
        )
        MODEL: str = Field(
            default="opencode/litellm/claude-sonnet-4-5",
            description=(
                "Model id forwarded to the gateway's /v1/responses. "
                "For the OpenCode backend use 'opencode/<provider>/<model>' "
                "where '<provider>/<model>' is one of OPENCODE_MODELS on the "
                "gateway (e.g. 'opencode/litellm/claude-sonnet-4-5', "
                "'opencode/openai/gpt-5.5'). Native Claude ids "
                "(sonnet/opus/haiku) also work."
            ),
        )
        TIMEOUT: int = Field(
            default=600,
            description="Total request timeout in seconds (increase for heavy MCP/search workloads)",
        )
        # Context injection settings
        INJECT_USER_CONTEXT: bool = Field(
            default=True,
            description="Inject user context (username as mlm_username) into prompt",
        )
        INJECT_CREDENTIALS: bool = Field(
            default=True,
            description="Fetch and inject credentials from Open WebUI for MCP authentication",
        )
        OPEN_WEBUI_URL: str = Field(
            default="http://host.docker.internal:10088",
            description="Open WebUI base URL for fetching credentials",
        )
        TOOL_DISPLAY: bool = Field(
            default=True,
            description="Show detailed tool blocks with args and result; when off, show a short status line instead",
        )
        MCP_TOOL_ONLY: bool = Field(
            default=False,
            description="Only display MCP tool results; hide all built-in SDK tools (Read, Bash, Edit, etc.)",
        )
        VQA_IMAGE_DIR: str = Field(
            default="/app/shared_images",
            description="Shared directory for saving uploaded images (must be mounted in both Open WebUI and gateway containers)",
        )
        IMAGE_SERVER_BASE: str = Field(
            default="",
            description="Base URL pattern for the image server (e.g. 'https://image-server.example.com'). "
                        "When set, image links matching this URL will trigger the gallery sidebar in Open WebUI.",
        )


        @field_validator("TOOL_DISPLAY", mode="before")
        @classmethod
        def _coerce_tool_display(cls, v):
            """Accept legacy string values from stored configs."""
            if isinstance(v, str):
                return v.lower() not in ("simple", "mcp_only", "false", "0", "no", "off")
            return v

    def __init__(self):
        self.valves = self.Valves()
        self._local = threading.local()
        # Track previous_response_id per chat for multi-turn continuity
        self._response_ids: dict[str, str] = {}

    def pipes(self) -> list:
        return [
            {
                "id": "chatdragon-responses-opencode",
                "name": "Chatdragon Responses (OpenCode)",
            }
        ]

    # ------------------------------------------------------------------
    # /v1/responses POST helpers
    # ------------------------------------------------------------------

    def _open_responses_stream(self, client, url, payload, chat_id):
        """Open a streaming POST to ``/v1/responses``, retrying once on
        409 ``Stale previous_response_id``.

        Returns a tuple ``(cm, resp)`` where ``cm`` is the active
        context-manager that the caller MUST close (``cm.__exit__``)
        when done iterating, and ``resp`` is the underlying
        ``httpx.Response`` ready for ``iter_lines()``.

        Raises ``Exception`` on any non-200 response that isn't a
        recoverable 409 stale.

        Why retry: even with the task-aware ``previous_response_id``
        chain skip in :meth:`pipe`, a stale 409 can still happen when
        an external writer (concurrent tab, server-side rehydrate,
        admin tool) advances the wrapper's response counter without
        us seeing it.  The 409 body includes the latest valid
        response_id, so a one-shot retry with the corrected payload
        recovers transparently.
        """
        for attempt in range(2):
            cm = client.stream(
                "POST", url, json=payload, headers=self._make_headers()
            )
            resp = cm.__enter__()
            if resp.status_code == 200:
                return cm, resp

            body_text = resp.read().decode()
            cm.__exit__(None, None, None)

            if (
                attempt == 0
                and resp.status_code == 409
                and chat_id
                and "previous_response_id" in payload
            ):
                m = _STALE_RESP_ID_RE.search(body_text)
                if m:
                    latest = f"resp_{m.group(1)}_{m.group(2)}"
                    log.warning(
                        "[PIPE] 409 stale prev=%s -> recovering with latest=%s for chat=%s",
                        payload.get("previous_response_id"),
                        latest,
                        chat_id,
                    )
                    self._response_ids[chat_id] = latest
                    payload["previous_response_id"] = latest
                    continue

            raise Exception(f"Server error ({resp.status_code}): {body_text}")

        # Defensive: the loop above either returns or raises.
        raise Exception("Stale 409 retry exhausted unexpectedly")

    # ------------------------------------------------------------------
    # Context injection
    # ------------------------------------------------------------------

    def _inject_context(
        self,
        text: str,
        __user__: Optional[dict],
        user_id: Optional[str] = None,
        cookies: Optional[dict] = None,
        dscrowd_token: Optional[str] = None,
        mlm_username: Optional[str] = None,
    ) -> str:
        """Inject user and credential context into the prompt text."""
        context_parts = []

        if self.valves.INJECT_USER_CONTEXT:
            if mlm_username:
                context_parts.append(f"<mlm_username>{mlm_username}</mlm_username>")
            elif __user__:
                user_name = __user__.get("name", "")
                if user_name:
                    context_parts.append(f"<mlm_username>{user_name}</mlm_username>")

        if self.valves.INJECT_CREDENTIALS:
            if dscrowd_token:
                context_parts.append(f"<dscrowd.token_key>{dscrowd_token}</dscrowd.token_key>")
            elif cookies:
                token = cookies.get("dscrowd.token_key")
                if token:
                    context_parts.append(f"<dscrowd.token_key>{token}</dscrowd.token_key>")

        if context_parts:
            return text + "\n\n" + "\n".join(context_parts)
        return text


    # ------------------------------------------------------------------
    # Image gallery detection
    # ------------------------------------------------------------------

    def _detect_image_gallery_urls(self, text: str) -> list[dict]:
        """Detect image URLs from IMAGE_SERVER_BASE in text and return gallery info."""
        if not self.valves.IMAGE_SERVER_BASE:
            return []

        base = self.valves.IMAGE_SERVER_BASE.rstrip("/")
        # Match URLs that look like image paths from the configured server
        # Pattern: base_url/path/to/folder/image.ext
        import re
        pattern = re.escape(base) + r"(/[^\s\)\"'<>]+\.(?:jpg|jpeg|png|gif|webp|bmp|tiff))"
        matches = re.findall(pattern, text, re.IGNORECASE)

        results = []
        seen_folders = set()
        for match in matches:
            import os.path
            folder = os.path.dirname(match)
            filename = os.path.basename(match)
            if folder not in seen_folders:
                seen_folders.add(folder)
                results.append({"folder": folder, "current": filename, "base_url": base})
        return results

    def _build_gallery_tag(
        self,
        folder: str = "",
        current: str = "",
        base_url: str = "",
        images: list[str] | None = None,
    ) -> str:
        """Build a <details type='image_gallery'> tag for the frontend.

        When *images* is provided the tag carries an inline JSON array of
        image URLs so the frontend can display them without an extra API
        call.  Otherwise the folder-based approach is used.
        """
        parts = ['type="image_gallery"', 'done="true"']
        if folder:
            parts.append(f'folder="{_safe_attr(folder)}"')
        if current:
            parts.append(f'current="{_safe_attr(current)}"')
        if base_url:
            parts.append(f'base_url="{_safe_attr(base_url)}"')
        if images:
            safe_images = _safe_attr(json.dumps(images, ensure_ascii=False))
            parts.append(f'images="{safe_images}"')
        attrs = " ".join(parts)
        return (
            f'\n\n<details {attrs}>\n'
            f'<summary>Image Gallery</summary>\n'
            f'</details>\n\n'
        )

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: list,
        body: dict,
    ):
        __user__ = body.get("user", {})
        __user_id__ = __user__.get("id", "")
        __metadata__ = body.get("metadata", {})
        __task__ = __metadata__.get("task")


        meta_headers = __metadata__.get("headers", {})
        log.info("[PIPE-DEBUG] body keys=%s", list(body.keys()))
        log.info("[PIPE-DEBUG] metadata keys=%s", list(__metadata__.keys()))
        log.info("[PIPE-DEBUG] meta_headers=%s", meta_headers)

        extra_headers: dict = {}

        dscrowd_token = meta_headers.get("x-cookie-dscrowd.token_key", "")
        if dscrowd_token:
            extra_headers["X-Cookie-dscrowd.token_key"] = dscrowd_token
            log.info("[PIPE] dscrowd_token: present (len=%d)", len(dscrowd_token))
        else:
            log.info("[PIPE] dscrowd_token: NOT FOUND")

        owui_username = meta_headers.get("x-openwebui-user-name", "")
        if not owui_username and __user__:
            email = __user__.get("email", "")
            if email and "@" in email:
                owui_username = email.split("@")[0]
            elif email:
                owui_username = email
        if owui_username:
            try:
                owui_username.encode("ascii")
                extra_headers["X-OpenWebUI-User-Name"] = owui_username
            except UnicodeEncodeError:
                from urllib.parse import quote
                extra_headers["X-OpenWebUI-User-Name"] = quote(owui_username)

        __cookies__ = body.get("cookies", {})
        if __cookies__ and not dscrowd_token:
            dscrowd_token = __cookies__.get("dscrowd.token_key", "")
            if dscrowd_token:
                extra_headers["X-Cookie-dscrowd.token_key"] = dscrowd_token

        self._local.extra_headers = extra_headers

        if not messages:
            return "No messages provided."

        # Build messages list — inject context into the last user message
        messages = list(messages)
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                content = messages[i].get("content", "")
                # Save uploaded images to shared volume and replace image_url
                # parts with text references so the text-only LLM can call the
                # VQA tool with the file path.
                if isinstance(content, list):
                    image_dir = Path(self.valves.VQA_IMAGE_DIR)
                    image_dir.mkdir(parents=True, exist_ok=True)
                    new_content = []
                    saved_paths: list[str] = []
                    for j, part in enumerate(content):
                        if isinstance(part, dict) and part.get("type") == "image_url":
                            url = ""
                            img_field = part.get("image_url", {})
                            if isinstance(img_field, dict):
                                url = img_field.get("url", "")
                            elif isinstance(img_field, str):
                                url = img_field
                            if url.startswith("data:image/"):
                                try:
                                    header, encoded = url.split(",", 1)
                                    # e.g. data:image/png;base64 -> png
                                    ext = header.split("/")[1].split(";")[0] if "/" in header else "png"
                                    filename = f"{uuid4().hex}.{ext}"
                                    filepath = image_dir / filename
                                    filepath.write_bytes(base64.b64decode(encoded))
                                    saved_paths.append(str(filepath))
                                    log.info("[IMAGE] saved image part[%d] -> %s", j, filepath)
                                except Exception:
                                    log.exception("[IMAGE] failed to save image part[%d]", j)
                                    new_content.append(part)
                            else:
                                # Non-base64 URL (http, file path, etc.) — keep as-is for VQA
                                saved_paths.append(url)
                                log.info("[IMAGE] non-base64 image part[%d] url=%s", j, url[:120])
                        else:
                            new_content.append(part)
                    if saved_paths:
                        paths_str = ", ".join(saved_paths)
                        hint = (
                            f"[사용자가 이미지를 업로드했습니다. 이미지 경로: {paths_str}. "
                            f"이미지 분석이 필요하면 vqa_search 도구를 호출하세요.]"
                        )
                        new_content.append({"type": "text", "text": hint})
                        content = new_content
                        messages[i] = {**messages[i], "content": content}
                        log.info("[IMAGE] rewrote message with %d image path(s)", len(saved_paths))
                if isinstance(content, str):
                    content = self._inject_context(
                        content,
                        __user__,
                        __user_id__,
                        __cookies__,
                        dscrowd_token=dscrowd_token or None,
                        mlm_username=owui_username or None,
                    )
                    messages[i] = {**messages[i], "content": content}
                elif isinstance(content, list):
                    # Multimodal content (e.g. image + text from VQA queries).
                    # Find the last text part and inject context into it.
                    last_text_idx = None
                    for j in range(len(content) - 1, -1, -1):
                        part = content[j]
                        if isinstance(part, dict) and part.get("type") == "text":
                            last_text_idx = j
                            break
                    if last_text_idx is not None:
                        text = content[last_text_idx].get("text", "")
                        text = self._inject_context(
                            text,
                            __user__,
                            __user_id__,
                            __cookies__,
                            dscrowd_token=dscrowd_token or None,
                            mlm_username=owui_username or None,
                        )
                        content = list(content)
                        content[last_text_idx] = {**content[last_text_idx], "text": text}
                    messages[i] = {**messages[i], "content": content}
                break

        use_stream = body.get("stream", True)
        chat_id = __metadata__.get("chat_id", "")

        # Extract the last user message as input for /v1/responses
        last_user_content = user_message
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                c = messages[i].get("content", "")
                if isinstance(c, str):
                    last_user_content = c
                elif isinstance(c, list):
                    # Extract text parts from multimodal content
                    parts = [p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"]
                    last_user_content = "\n".join(parts)
                break

        prev_resp_id = self._response_ids.get(chat_id) if chat_id else None

        payload = {
            "model": self.valves.MODEL,
            "input": last_user_content,
            "stream": use_stream,
        }

        # Multi-turn: chain ``previous_response_id`` for normal chat
        # turns only.  Task requests (title generation, follow-up
        # suggestions, …) get sent as standalone calls — chaining
        # them would (a) advance the wrapper's response counter and
        # leave the chat's stored ``response_id`` stale, producing a
        # 409 ``Stale previous_response_id`` on the next user turn,
        # and (b) pollute the chat's conversation history with the
        # task's prompt and reply.  Open WebUI already embeds the
        # full chat content into the task prompt, so dropping the
        # chain costs nothing.
        if prev_resp_id and not __task__:
            payload["previous_response_id"] = prev_resp_id
        elif not __task__:
            # First turn: include system instructions if any
            system_msg = next(
                (m.get("content", "") for m in messages if m.get("role") == "system"),
                None,
            )
            if system_msg:
                payload["instructions"] = system_msg

        # User identity for workspace isolation
        if owui_username:
            payload["user"] = owui_username

        # Pass selected MCP tools to gateway as allowed_tools
        mcp_tools = body.get("mcp_tools") or __metadata__.get("mcp_tools")
        if mcp_tools and isinstance(mcp_tools, list):
            base_tools = ["Read", "Glob", "Grep", "Bash", "Write", "Edit", "Skill"]
            payload["allowed_tools"] = base_tools + mcp_tools
            log.info("[PIPE] allowed_tools: %s", payload["allowed_tools"])

        if use_stream:
            return self._stream(payload, __task__, chat_id)
        else:
            return self._non_stream(payload, __task__, chat_id)

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def _stream(self, payload: dict, task: Optional[str], chat_id: str = "") -> Iterator[str]:
        full_text_acc = ""  # Accumulate full response for image URL detection

        # ── <think>/<details> isolation state ──────────────────────────
        # ``block_state`` is the pipe's *output* state: either we have
        # an open <think> block we still need to close ("THINKING") or
        # nothing is open ("IDLE").  Tool ``<details>`` blocks are
        # always emitted atomically (open and close in one yield)
        # after a forced ``goto(IDLE)``, so they never live in the
        # output state.
        #
        # ``upstream_in_think`` mirrors what the *upstream* stream is
        # currently inside, parsed by a streaming tag scanner with a
        # 7-char hold-back buffer (``holdback``) for tags that split
        # across SSE deltas (e.g. ``"<thi"`` then ``"nk>"``).  We
        # strip upstream <think>/</think> from the visible stream
        # entirely and re-emit canonical pairs from ``block_state``,
        # so unbalanced or stale upstream tags can never leak through.
        block_state = "IDLE"
        upstream_in_think = False
        holdback = ""
        HOLDBACK_LEN = 7  # max(len("<think>"), len("</think>")) - 1

        def goto_idle() -> str:
            nonlocal block_state
            if block_state == "THINKING":
                block_state = "IDLE"
                return "\n</think>\n\n"
            return ""

        def goto_thinking() -> str:
            nonlocal block_state
            if block_state == "IDLE":
                block_state = "THINKING"
                return "<think>\n"
            return ""

        def scan_chunk(chunk: str) -> list:
            """Strip upstream <think>/</think> from *chunk* and return
            a list of (kind, text) where ``kind`` is ``"reasoning"``
            (was inside <think>) or ``"text"`` (outside).  Updates
            ``upstream_in_think`` and ``holdback`` as a side effect.
            """
            nonlocal upstream_in_think, holdback
            buf = holdback + chunk
            holdback = ""

            # Hold back any trailing chars that might be the start of
            # a tag we haven't seen the end of yet.
            safe_end = len(buf)
            for hb_len in range(min(HOLDBACK_LEN, len(buf)), 0, -1):
                tail = buf[-hb_len:]
                if "<think>".startswith(tail) or "</think>".startswith(tail):
                    safe_end = len(buf) - hb_len
                    break
            holdback = buf[safe_end:]
            safe = buf[:safe_end]

            out = []
            i = 0
            n = len(safe)
            while i < n:
                if upstream_in_think:
                    idx = safe.find("</think>", i)
                    if idx == -1:
                        out.append(("reasoning", safe[i:]))
                        i = n
                    else:
                        if idx > i:
                            out.append(("reasoning", safe[i:idx]))
                        upstream_in_think = False
                        i = idx + len("</think>")
                else:
                    idx = safe.find("<think>", i)
                    if idx == -1:
                        out.append(("text", safe[i:]))
                        i = n
                    else:
                        if idx > i:
                            out.append(("text", safe[i:idx]))
                        upstream_in_think = True
                        i = idx + len("<think>")
            return out

        def flush_holdback_segments() -> list:
            """Emit pending holdback as a single segment of the
            current upstream kind.  Called at boundaries (tool events,
            end-of-stream) where no further chars can extend a
            partial tag — we know whatever is in ``holdback`` is
            literal content of the current upstream-think state.
            """
            nonlocal holdback
            if not holdback:
                return []
            kind = "reasoning" if upstream_in_think else "text"
            text = holdback
            holdback = ""
            return [(kind, text)] if text else []

        def emit_segments(segments):
            """Yield strings for each (kind, text) segment, switching
            block_state as needed.  Generator helper used by both the
            text-delta path and the boundary-flush path.
            """
            for kind, text in segments:
                if not text:
                    continue
                if kind == "reasoning":
                    yield goto_thinking()
                else:
                    yield goto_idle()
                yield text

        tool_names: dict = {}
        tool_pending: dict = {}
        any_tool_used = False
        collected_thumbnails: list[str] = []  # Thumbnails from MCP tool results
        # Tool explorer: {tool_label: [{query, results}]}
        tool_explorer_data: dict[str, list[dict]] = {}
        try:
            url = f"{self.valves.BASE_URL.rstrip('/')}/v1/responses"
            timeout = httpx.Timeout(
                connect=30.0,
                read=float(self.valves.TIMEOUT),
                write=30.0,
                pool=30.0,
            )
            with httpx.Client(timeout=timeout) as client:
                resp_cm, resp = self._open_responses_stream(client, url, payload, chat_id)
                try:

                    # Responses API uses SSE with event: type\ndata: json
                    current_event_type = ""
                    for line in resp.iter_lines():
                        # SSE keepalive comments
                        if line.startswith(":"):
                            continue
                        # Event type line
                        if line.startswith("event: "):
                            current_event_type = line[7:].strip()
                            continue
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break

                        try:
                            event = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue

                        event_type = event.get("type", current_event_type)
                        log.info("[PIPE-DEBUG] event_type=%s", event_type)

                        # Save response ID for multi-turn continuity.
                        # Skip for task requests: a task response advances
                        # the wrapper-side counter but its response_id
                        # is not the one a subsequent chat turn should
                        # chain off — saving it would mix task prompts
                        # into the chat history and (worse) eventually
                        # produce a 409 Stale previous_response_id on
                        # the next chat turn.
                        if event_type == "response.completed":
                            resp_obj = event.get("response", {})
                            resp_id = resp_obj.get("id", "")
                            if resp_id and chat_id and not task:
                                self._response_ids[chat_id] = resp_id
                                log.info("[PIPE] saved response_id=%s for chat=%s", resp_id, chat_id)
                            # Note: this pipe variant intentionally does
                            # not surface ``status=requires_action`` as a
                            # card.  AskUserQuestion / sensitive-file
                            # prompts therefore stall the model silently
                            # — that is the trade-off this variant makes
                            # for stability.  Switch to the regular
                            # ``chatdragon_responses`` pipe to get the
                            # interactive card flow.
                            continue

                        if event_type == "response.failed":
                            err = event.get("response", {}).get("error", {})
                            err_msg = err.get("message", "Unknown error")
                            log.error("[PIPE] response.failed: %s", err_msg)
                            for s in emit_segments(flush_holdback_segments()):
                                yield s
                            yield goto_idle()
                            yield f"\n\nError: {err_msg}"
                            continue

                        # Skip non-content lifecycle events
                        if event_type in (
                            "response.created", "response.in_progress",
                            "response.output_item.added", "response.output_item.done",
                            "response.content_part.added", "response.content_part.done",
                            "response.output_text.done",
                        ):
                            continue

                        # Handle tool events (same structure as old system_event)
                        sys_event = None
                        if event_type == "response.tool_use":
                            sys_event = event
                            sys_event["type"] = "tool_use"
                        elif event_type == "response.tool_result":
                            sys_event = event
                            sys_event["type"] = "tool_result"
                        elif event_type.startswith("response.task_"):
                            sys_event = event
                            sys_event["type"] = event_type.replace("response.", "")

                        if sys_event:
                            event_type = sys_event.get("type", "")
                            log.info(
                                "[PIPE] system_event type=%s keys=%s",
                                event_type, list(sys_event.keys()),
                            )
                            if event_type in ("tool_use", "tool_result"):
                                any_tool_used = True
                                log.info(
                                    "[PIPE-DEBUG] %s raw_event=%s",
                                    event_type, json.dumps(sys_event, default=str)[:500],
                                )
                            # Extract data from MCP tool results
                            if event_type == "tool_result":
                                tool_id = sys_event.get("tool_use_id", "")
                                raw = (
                                    sys_event.get("content", "")
                                    or sys_event.get("output", "")
                                    or sys_event.get("result", "")
                                )
                                raw_str = str(raw)
                                log.info(
                                    "[PIPE-PARSE] raw type=%s len=%s preview=%s",
                                    type(raw).__name__,
                                    len(raw_str),
                                    raw_str[:300],
                                )
                                # Detect persisted-output: SDK saved large
                                # result to file and will Read it next.
                                t_name = tool_names.get(tool_id, "")
                                is_persisted = "[persisted-output]" in raw_str or "Output too large" in raw_str
                                if is_persisted and is_mcp_tool(t_name):
                                    # Extract file path from persisted-output message
                                    path_match = re.search(r"saved to:\s*(\S+)", raw_str)
                                    persisted_path = path_match.group(1) if path_match else ""
                                    # Store {file_path: (tool_name, args)} for matching
                                    if not hasattr(self._local, "_persisted_map"):
                                        self._local._persisted_map = {}
                                    pending_info = tool_pending.get(tool_id, {})
                                    self._local._persisted_map[persisted_path] = {
                                        "tool": t_name,
                                        "args": pending_info.get("args", "{}"),
                                    }
                                    log.info(
                                        "[PIPE-PARSE] persisted-output detected tool=%s path=%s",
                                        t_name, persisted_path,
                                    )
                                else:
                                    # Check if this Read's file_path matches a persisted-output
                                    persisted_map = getattr(self._local, "_persisted_map", {})
                                    persisted_match = None
                                    if t_name == "Read" or not is_mcp_tool(t_name):
                                        # Check tool_use args for file_path
                                        read_args = tool_pending.get(tool_id, {}).get("args", "{}")
                                        try:
                                            read_parsed = json.loads(read_args)
                                            read_path = read_parsed.get("file_path", "")
                                        except (json.JSONDecodeError, AttributeError):
                                            read_path = ""
                                        if read_path and read_path in persisted_map:
                                            persisted_match = persisted_map.pop(read_path)
                                            t_name = persisted_match["tool"]
                                            log.info(
                                                "[PIPE-PARSE] Read file_path=%s matched persisted tool=%s",
                                                read_path, t_name,
                                            )

                                    parsed = parse_tool_content(raw)
                                    log.info(
                                        "[PIPE-PARSE] parsed type=%s result=%s",
                                        type(parsed).__name__ if parsed else "None",
                                        str(parsed)[:300] if parsed else "None",
                                    )
                                    # Thumbnails for gallery
                                    thumbs = extract_thumbnails_from_tool_result(raw)
                                    if thumbs:
                                        collected_thumbnails.extend(thumbs)
                                        log.info("[PIPE] collected %d thumbnails", len(thumbs))
                                    # Structured results for tool explorer
                                    if is_mcp_tool(t_name):
                                        results = extract_tool_results_for_explorer(raw)
                                        if results:
                                            label = mcp_label_key(t_name)
                                            # Get query from args
                                            orig_args = persisted_match["args"] if persisted_match else ""
                                            pending = tool_pending.get(tool_id, {})
                                            query = orig_args or pending.get("args", "{}")
                                            try:
                                                q_parsed = json.loads(query)
                                                # Extract readable search query
                                                query_str = ""
                                                for v in q_parsed.values():
                                                    if isinstance(v, str) and len(v) > 2:
                                                        query_str = v
                                                        break
                                                if query_str:
                                                    query = query_str
                                                else:
                                                    # No obvious string value; show key=value pairs
                                                    pairs = [
                                                        f"{k}={v}" for k, v in q_parsed.items()
                                                        if isinstance(v, (str, int, float)) and str(v).strip()
                                                    ]
                                                    query = ", ".join(pairs) if pairs else query
                                            except (json.JSONDecodeError, AttributeError):
                                                pass
                                            call_data = {
                                                "query": query[:200],
                                                "results": results,
                                            }
                                            # Track for dedup
                                            if label not in tool_explorer_data:
                                                tool_explorer_data[label] = []
                                            tool_explorer_data[label].append(call_data)
                                            # Emit immediately so sidebar updates live
                                            explorer_tag = build_tool_explorer_tag(
                                                {label: [call_data]}
                                            )
                                            # Boundary: flush holdback as
                                            # the current upstream state,
                                            # then close any open <think>
                                            # before the atomic explorer
                                            # <details>.  Without goto(IDLE)
                                            # the explorer would render
                                            # nested inside the collapsed
                                            # thought.
                                            for s in emit_segments(flush_holdback_segments()):
                                                yield s
                                            yield goto_idle()
                                            yield explorer_tag
                                            log.info(
                                                "[PIPE] tool_explorer: %s +%d results (live)",
                                                label, len(results),
                                            )
                                    # (persisted_map entries auto-removed via .pop above)
                            rendered = self._render_system_event(
                                event_type, sys_event, tool_names, tool_pending,
                            )
                            if rendered:
                                # Same boundary protocol: flush the
                                # holdback as text/reasoning, then
                                # goto(IDLE) so the atomic tool
                                # <details> can never nest inside an
                                # open <think>.
                                for s in emit_segments(flush_holdback_segments()):
                                    yield s
                                yield goto_idle()
                                yield rendered
                            continue

                        # Text delta handling
                        if event_type != "response.output_text.delta":
                            continue
                        chunk = event.get("delta", "")
                        if not chunk:
                            continue

                        # Run the chunk through the streaming tag
                        # scanner.  This strips upstream <think>/</think>
                        # entirely, classifies the surviving text as
                        # reasoning (was inside <think>) or text
                        # (outside), and re-emits canonical pairs from
                        # block_state.  Tags that split across deltas
                        # are reassembled via ``holdback``.
                        full_text_acc += chunk
                        for s in emit_segments(scan_chunk(chunk)):
                            yield s
                finally:
                    # Close the streaming response we opened via
                    # _open_responses_stream — its context manager isn't
                    # bound to a ``with`` here so we close it manually.
                    resp_cm.__exit__(None, None, None)

        except Exception as e:
            log.error("Stream error: %s", e)
            # Close any open <think> so the error message renders as
            # plain text and isn't swallowed by a collapsed thought.
            yield goto_idle()
            yield f"\n\nError: {e}"
        finally:
            # End-of-stream flush.  Anything sitting in ``holdback``
            # is now known to be literal content (no further chars can
            # extend a partial tag), so emit it under the current
            # upstream-think state.  Then ``goto(IDLE)`` so the
            # deferred final-flush blocks below can't land inside an
            # open <think>.
            for s in emit_segments(flush_holdback_segments()):
                yield s
            yield goto_idle()

            # (tool_explorer tags emitted live during streaming)

            # Emit final "검색된 문서 보기" button with all collected results
            if tool_explorer_data:
                body = json.dumps(tool_explorer_data, ensure_ascii=False)
                yield (
                    f'\n\n<details type="search_results_button" done="true">\n'
                    f'<summary>Search Results</summary>\n'
                    f'{body}\n'
                    f'</details>\n\n'
                )

            # Emit image gallery for collected MCP thumbnails
            if collected_thumbnails:
                yield self._build_gallery_tag(images=collected_thumbnails)

            # Emit image gallery tags for any IMAGE_SERVER_BASE URLs found
            gallery_matches = self._detect_image_gallery_urls(full_text_acc)
            for match in gallery_matches:
                yield self._build_gallery_tag(
                    folder=match["folder"],
                    current=match["current"],
                    base_url=match["base_url"],
                )

    def _render_system_event(
        self,
        event_type: str,
        event: dict,
        tool_names: dict,
        tool_pending: dict,
    ) -> Optional[str]:
        """Render a system_event into display text (tool blocks, task progress)."""

        if event_type == "task_started":
            desc = event.get("description", "")
            if desc:
                return f"\n\n> **Task**: {desc}\n"

        elif event_type == "task_progress":
            desc = event.get("description", "")
            tool = event.get("last_tool_name", "")
            usage = event.get("usage") or {}
            uses = usage.get("tool_uses", 0)
            text = f"\n> **Progress**: {desc}"
            if tool:
                text += f" ({tool}, {uses} uses)"
            return text + "\n"

        elif event_type == "task_notification":
            status = event.get("status", "")
            summary = event.get("summary", "")
            if summary:
                return f"\n> **Task {status}**: {summary}\n\n"

        elif event_type == "tool_use":
            log.info("[PIPE] tool_use event keys=%s", list(event.keys()))
            tool_id = event.get("tool_use_id", event.get("id", ""))
            name = event.get("name", "")
            if tool_id:
                tool_names[tool_id] = name
            tool_args = json.dumps(
                event.get("input", event.get("arguments", {})),
                ensure_ascii=False,
            )
            tool_pending[tool_id] = {"name": name, "args": tool_args}

        elif event_type == "tool_result":
            tool_id = event.get("tool_use_id", "")
            pending = tool_pending.pop(tool_id, {})
            name = pending.get("name", tool_names.get(tool_id, ""))
            args = pending.get("args", "{}")
            is_error = event.get("is_error", False)
            raw_content = event.get("content", "") or event.get("output", "") or event.get("result", "")
            log.info(
                "[PIPE] tool_result id=%s name=%s content_type=%s content_preview=%s",
                tool_id, name, type(raw_content).__name__,
                str(raw_content)[:300],
            )
            result_content = extract_tool_result_text(raw_content)
            if not result_content and is_error:
                result_content = event.get("error", "Tool execution failed")
            # SDK overflow: shorten the verbose message.
            if result_content.startswith("Error: result ("):
                m = re.search(r"\(([0-9,]+) characters?\)", result_content)
                chars = m.group(1) if m else "large"
                result_content = f"Result truncated ({chars} chars)"
            result_content = result_content[:10000]
            esc_name = html.escape(name)

            if self.valves.MCP_TOOL_ONLY and not is_mcp_tool(name):
                return None

            if not self.valves.TOOL_DISPLAY:
                friendly = friendly_tool_notification(name, is_error)
                details_tag = f"\n> {friendly}\n"
            else:
                safe_args = _safe_attr(args)
                safe_result = _safe_attr(result_content)
                details_tag = (
                    f'\n\n<details type="tool_calls"'
                    f' name="{esc_name}"'
                    f' arguments="{safe_args}"'
                    f' result="{safe_result}"'
                    f' done="true">\n'
                    f"<summary>Tool: {esc_name}</summary>\n"
                    f"</details>\n\n"
                )
                log.info(
                    "[PIPE-DEBUG] tool_id=%s name=%s args_len=%d result_len=%d",
                    tool_id, name, len(safe_args), len(safe_result),
                )
                log.info("[PIPE-DEBUG] raw_args=%s", args[:500])
                log.info("[PIPE-DEBUG] safe_args=%s", safe_args[:500])
                log.info("[PIPE-DEBUG] result_preview=%s", result_content[:500])
                log.info("[PIPE-DEBUG] safe_result_preview=%s", safe_result[:500])
            log.info("[PIPE-DEBUG] details_tag_first_300=%s", details_tag[:300])
            return details_tag

        return None

    def _non_stream(self, payload: dict, task: Optional[str], chat_id: str = "") -> str:
        url = f"{self.valves.BASE_URL.rstrip('/')}/v1/responses"
        payload["stream"] = False
        try:
            with httpx.Client(timeout=httpx.Timeout(self.valves.TIMEOUT)) as client:
                resp = client.post(url, json=payload, headers=self._make_headers())
                if resp.status_code != 200:
                    return f"Error: Server error ({resp.status_code}): {resp.text}"

                data = resp.json()
                # Save response ID — skip for task requests so task
                # responses don't displace the chat's chained response
                # and trigger a 409 on the next user turn (see _stream
                # for the full reasoning).
                resp_id = data.get("id", "")
                if resp_id and chat_id and not task:
                    self._response_ids[chat_id] = resp_id

                # Extract text from output items
                output = data.get("output", [])
                content = ""
                for item in output:
                    if item.get("type") == "message":
                        for part in item.get("content", []):
                            if part.get("type") == "output_text":
                                content += part.get("text", "")

                return content
        except Exception as e:
            log.error("Non-stream error: %s", e)
            return f"Error: {e}"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.valves.API_KEY:
            headers["Authorization"] = f"Bearer {self.valves.API_KEY}"
        extra = getattr(self._local, "extra_headers", None)
        if extra:
            headers.update(extra)
        return headers
