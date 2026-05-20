"""
title: ChatDragon Minimal (diagnostic)
author: claude-code-openai-wrapper
version: 0.7.0
description: |
    Bare-minimum /v1/responses pipe used to diagnose where the
    feature-rich chatdragon_responses_* pipes inject behavior that
    breaks subagent output. Layered incrementally so each feature
    added back is its own commit:

      v0.1.0  text-only forwarding (response.output_text.delta)
      v0.2.0  + tool_use / tool_result / task_* rendering
      v0.3.0  + previous_response_id chaining per chat_id
      v0.5.0  cleanup pass
      v0.6.0  + payload.user (workspace isolation)
      v0.7.0  rollback to v0.2.0 functionality — drop user and
              previous_response_id so we can re-isolate which
              addition is making subagents return empty. The
              streaming-correct refactor (pipe() returns
              _stream(), persistent httpx client, pipes()
              registration) is kept.

    Currently omits: allowed_tools, instructions, context
    injection (mlm_username / dscrowd token), MEMORY.md guidance,
    thought_wrapped / <response> token, user identity,
    previous_response_id chaining.

    DEBUG_RAW=true dumps every SSE chunk as JSON instead of
    rendering — useful when chunk timing / shape is what you want
    to inspect.
license: MIT
"""

import html
import json
import logging
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
        messages: list,
        body: dict,
    ):
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

        log.info("[MINIMAL] POST %s/v1/responses payload=%s", self.valves.BASE_URL, payload)

        return self._stream(payload)

    # ------------------------------------------------------------------
    # Streaming generator
    # ------------------------------------------------------------------

    def _stream(self, payload: dict) -> Iterator[str]:
        # tool_use comes before tool_result; buffer name+args until result arrives.
        tool_pending: Dict[str, Dict[str, str]] = {}

        url = f"{self.valves.BASE_URL.rstrip('/')}/v1/responses"
        timeout = httpx.Timeout(
            connect=30.0,
            read=float(self.valves.TIMEOUT),
            write=30.0,
            pool=30.0,
        )
        try:
            with httpx.Client(timeout=timeout) as client:
                with client.stream(
                    "POST",
                    url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as resp:
                    if resp.status_code != 200:
                        body_text = resp.read().decode("utf-8", errors="replace")
                        log.error(
                            "[MINIMAL] gateway returned %s: %s",
                            resp.status_code, body_text[:500],
                        )
                        yield f"\n[gateway error {resp.status_code}] {body_text[:500]}\n"
                        return

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

                        if self.valves.DEBUG_RAW:
                            payload_str = json.dumps(
                                {"event": current_event_type, "chunk": event},
                                ensure_ascii=False,
                            )
                            if len(payload_str) > 2000:
                                payload_str = payload_str[:2000] + "...(truncated)"
                            yield f"\n```json\n{payload_str}\n```\n"
                            continue

                        if event_type == "response.completed":
                            continue

                        if event_type == "response.failed":
                            err = event.get("response", {}).get("error", {})
                            err_msg = err.get("message", "Unknown error")
                            yield f"\n\nError: {err_msg}"
                            continue

                        # Skip non-content lifecycle events.
                        if event_type in (
                            "response.created", "response.in_progress",
                            "response.output_item.added", "response.output_item.done",
                            "response.content_part.added", "response.content_part.done",
                            "response.output_text.done",
                        ):
                            continue

                        if event_type == "response.tool_use":
                            tool_id = event.get("tool_use_id") or event.get("id") or ""
                            if tool_id:
                                tool_pending[tool_id] = {
                                    "name": event.get("name", ""),
                                    "args": json.dumps(
                                        event.get("input", event.get("arguments", {})),
                                        ensure_ascii=False,
                                    ),
                                }
                            continue

                        if event_type == "response.tool_result":
                            rendered = self._render_tool_result(event, tool_pending)
                            if rendered:
                                yield rendered
                            continue

                        if event_type == "response.task_started":
                            rendered = self._render_task_started(event)
                            if rendered:
                                yield rendered
                            continue

                        if event_type == "response.task_progress":
                            rendered = self._render_task_progress(event)
                            if rendered:
                                yield rendered
                            continue

                        if event_type == "response.task_notification":
                            rendered = self._render_task_notification(event)
                            if rendered:
                                yield rendered
                            continue

                        # Text delta — the main streaming path. Yield the raw
                        # delta so each token surfaces independently.
                        if event_type != "response.output_text.delta":
                            continue
                        chunk = event.get("delta", "")
                        if not chunk:
                            continue
                        yield chunk
        except httpx.HTTPError as e:
            log.exception("[MINIMAL] gateway call failed")
            yield f"\n[transport error] {e!s}\n"
