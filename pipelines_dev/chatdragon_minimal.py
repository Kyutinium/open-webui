"""
title: ChatDragon Minimal (diagnostic)
author: claude-code-openai-wrapper
version: 0.3.0
description: |
    Bare-minimum /v1/responses pipe for diagnosing why feature-rich
    pipes break subagent output.

    Adds previous_response_id chaining on top of v0.2.0:
    - Captures response.id from response.completed events.
    - Sends payload["previous_response_id"] on subsequent turns of
      the same chat so the gateway reuses the session (and the
      orchestrator's accumulated context / workspace).
    - Skips chaining when metadata.task is set (title generation,
      follow-up suggestions, etc.) — those one-shot calls should
      not advance the chat's response counter.
    - No 409 stale recovery (deliberately omitted — if the bisect
      shows chaining breaks subagents, that's our answer).

    Still omits: allowed_tools, instructions, context injection,
    MEMORY.md / <response> guidance, thought_wrapped, user id.

    DEBUG_RAW=true to dump every SSE chunk as JSON instead.
license: MIT
"""

import html
import json
import logging
import time
from typing import Any, Dict, Iterator, Optional

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


def _safe_attr(value: str) -> str:
    """Sanitize for a double-quoted HTML attribute value."""
    return (
        value.replace("&", "+")
        .replace('"', "'")
        .replace("<", "[")
        .replace(">", "]")
        .replace("\n", " ")
        .replace("\r", "")
    )


class Pipeline:
    class Valves(BaseModel):
        BASE_URL: str = Field(
            default="http://host.docker.internal:17995",
            description="Claude Code Gateway server URL",
        )
        MODEL: str = Field(
            default="sonnet",
            description="Claude model (sonnet / opus / haiku)",
        )
        TIMEOUT: int = Field(default=600)
        DEBUG_RAW: bool = Field(
            default=False,
            description="Dump every SSE chunk as JSON instead of rendering",
        )
        TOOL_RESULT_LIMIT: int = Field(
            default=10000,
            description="Max chars of tool_result content rendered into the details block",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()
        # chat_id → last response_id captured from response.completed.
        # In-memory only; multi-worker deployments would need a shared
        # store but that's out of scope for a diagnostic pipe.
        self._response_ids: Dict[str, str] = {}

    def pipes(self) -> list[dict]:
        return [
            {
                "id": "chatdragon-minimal",
                "name": "ChatDragon Minimal (diagnostic)",
            }
        ]

    # ------------------------------------------------------------------
    # Event renderers
    # ------------------------------------------------------------------

    def _render_task_started(self, chunk: Dict[str, Any]) -> Optional[str]:
        desc = chunk.get("description", "")
        if not desc:
            return None
        return f"\n\n> **Task**: {desc}\n"

    def _render_task_progress(self, chunk: Dict[str, Any]) -> Optional[str]:
        desc = chunk.get("description", "")
        tool = chunk.get("last_tool_name", "")
        usage = chunk.get("usage") or {}
        uses = usage.get("tool_uses", 0)
        text = f"\n> **Progress**: {desc}"
        if tool:
            text += f" ({tool}, {uses} uses)"
        return text + "\n"

    def _render_task_notification(self, chunk: Dict[str, Any]) -> Optional[str]:
        status = chunk.get("status", "")
        summary = chunk.get("summary", "")
        if not summary:
            return None
        return f"\n> **Task {status}**: {summary}\n\n"

    def _render_tool_result(
        self,
        chunk: Dict[str, Any],
        pending: Dict[str, Dict[str, str]],
    ) -> Optional[str]:
        tool_id = chunk.get("tool_use_id", "")
        meta = pending.pop(tool_id, {})
        name = meta.get("name", "")
        args = meta.get("args", "{}")
        is_error = bool(chunk.get("is_error", False))
        raw_content = chunk.get("content", "") or ""
        if isinstance(raw_content, list):
            # MCP results often arrive as [{"type": "text", "text": "..."}]
            parts = []
            for p in raw_content:
                if isinstance(p, dict):
                    parts.append(p.get("text", "") or json.dumps(p, ensure_ascii=False))
                else:
                    parts.append(str(p))
            raw_content = "\n".join(parts)
        elif not isinstance(raw_content, str):
            raw_content = json.dumps(raw_content, ensure_ascii=False)
        if not raw_content and is_error:
            raw_content = chunk.get("error", "Tool execution failed")
        raw_content = raw_content[: self.valves.TOOL_RESULT_LIMIT]

        esc_name = html.escape(name or "tool")
        safe_args = _safe_attr(args)
        safe_result = _safe_attr(raw_content)
        return (
            f'\n\n<details type="tool_calls"'
            f' name="{esc_name}"'
            f' arguments="{safe_args}"'
            f' result="{safe_result}"'
            f' done="true">\n'
            f"<summary>Tool: {esc_name}</summary>\n"
            f"</details>\n\n"
        )

    # ------------------------------------------------------------------
    # Pipe entry point
    # ------------------------------------------------------------------

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: list[dict],
        body: dict,
    ) -> Iterator[str]:
        metadata = body.get("metadata") or {}
        chat_id = metadata.get("chat_id", "") or ""
        task = metadata.get("task")  # title-gen / follow-up etc.

        last_user_content = user_message
        for m in reversed(messages):
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, str):
                    last_user_content = c
                elif isinstance(c, list):
                    last_user_content = "\n".join(
                        p.get("text", "")
                        for p in c
                        if isinstance(p, dict) and p.get("type") == "text"
                    )
                break

        payload = {
            "model": self.valves.MODEL,
            "input": last_user_content,
            "stream": True,
        }

        # Chain to the previous response for multi-turn continuity, but
        # only for real chat turns — title/follow-up tasks share the
        # chat_id and would otherwise advance the response counter,
        # leaving the next user turn with a stale previous_response_id.
        prev_resp_id = self._response_ids.get(chat_id) if chat_id else None
        if prev_resp_id and not task:
            payload["previous_response_id"] = prev_resp_id

        log.info(
            "[MINIMAL] POST %s/v1/responses chat_id=%s prev=%s task=%s payload=%s",
            self.valves.BASE_URL, chat_id, prev_resp_id, task, payload,
        )

        # tool_use comes before tool_result; buffer name+args until result arrives.
        tool_pending: Dict[str, Dict[str, str]] = {}

        # Use a persistent client with per-phase timeouts so the read phase can
        # block on the long-tailed SSE stream without idle disconnects, while
        # connect/write/pool stay snappy. Matches the streaming behavior of the
        # feature-rich pipe — bare httpx.stream() sometimes buffers larger
        # chunks before the first iter_lines() yield.
        timeout = httpx.Timeout(
            connect=30.0,
            read=float(self.valves.TIMEOUT),
            write=30.0,
            pool=30.0,
        )
        try:
            with httpx.Client(timeout=timeout) as client, client.stream(
                "POST",
                f"{self.valves.BASE_URL}/v1/responses",
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as resp:
                if resp.status_code != 200:
                    body_text = resp.read().decode("utf-8", errors="replace")
                    log.error("[MINIMAL] gateway returned %s: %s", resp.status_code, body_text[:500])
                    yield f"\n[gateway error {resp.status_code}] {body_text[:500]}\n"
                    return

                event_name: Optional[str] = None
                for raw_line in resp.iter_lines():
                    if raw_line is None:
                        continue
                    line = raw_line if isinstance(raw_line, str) else raw_line.decode("utf-8", errors="replace")
                    if not line:
                        event_name = None
                        continue
                    if line.startswith("event:"):
                        event_name = line[len("event:"):].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    data_str = line[len("data:"):].strip()
                    if data_str in ("", "[DONE]"):
                        continue
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    chunk_type = chunk.get("type") or event_name

                    if self.valves.DEBUG_RAW:
                        payload_str = json.dumps(
                            {"event": event_name, "chunk": chunk},
                            ensure_ascii=False,
                        )
                        if len(payload_str) > 2000:
                            payload_str = payload_str[:2000] + "...(truncated)"
                        yield f"\n```json\n{payload_str}\n```\n"
                        continue

                    if chunk_type == "response.output_text.delta":
                        delta = chunk.get("delta", "")
                        if isinstance(delta, str) and delta:
                            yield delta
                            # Open WebUI runs sync pipe() in a thread executor
                            # and pushes each yield onto an asyncio queue. A
                            # too-fast generator fills that queue before the
                            # event loop gets to flush HTTP chunks to the
                            # client, so deltas arrive in visible bursts. The
                            # feature-rich pipe avoids this incidentally by
                            # doing extra per-delta work (regex tool-noise
                            # filter, string accumulation). A bare sleep(0)
                            # achieves the same — releases the GIL and lets
                            # the asyncio loop ship the chunk before we yield
                            # the next one.
                            time.sleep(0)
                        continue

                    if chunk_type == "response.tool_use":
                        tool_id = chunk.get("tool_use_id") or chunk.get("id") or ""
                        if tool_id:
                            tool_pending[tool_id] = {
                                "name": chunk.get("name", ""),
                                "args": json.dumps(
                                    chunk.get("input", chunk.get("arguments", {})),
                                    ensure_ascii=False,
                                ),
                            }
                        continue

                    if chunk_type == "response.tool_result":
                        rendered = self._render_tool_result(chunk, tool_pending)
                        if rendered:
                            yield rendered
                        continue

                    if chunk_type == "response.task_started":
                        rendered = self._render_task_started(chunk)
                        if rendered:
                            yield rendered
                        continue

                    if chunk_type == "response.task_progress":
                        rendered = self._render_task_progress(chunk)
                        if rendered:
                            yield rendered
                        continue

                    if chunk_type == "response.task_notification":
                        rendered = self._render_task_notification(chunk)
                        if rendered:
                            yield rendered
                        continue

                    if chunk_type == "response.completed":
                        # Capture the final response id so the next turn on
                        # this chat can chain via previous_response_id.
                        # Skip task calls — they share the chat_id and would
                        # poison the chain.
                        if chat_id and not task:
                            new_id = (chunk.get("response") or {}).get("id")
                            if new_id:
                                self._response_ids[chat_id] = new_id
                                log.info(
                                    "[MINIMAL] captured response_id=%s for chat=%s",
                                    new_id, chat_id,
                                )
                        continue

                    if chunk_type in ("response.failed", "response.error"):
                        yield f"\n[gateway: {chunk_type}] {json.dumps(chunk, ensure_ascii=False)[:500]}\n"
                        continue
        except httpx.HTTPError as e:
            log.exception("[MINIMAL] gateway call failed")
            yield f"\n[transport error] {e!s}\n"
