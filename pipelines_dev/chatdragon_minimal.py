"""
title: ChatDragon Minimal (diagnostic)
author: claude-code-openai-wrapper
version: 0.1.0
description: |
    Bare-minimum /v1/responses pipe for diagnosing why feature-rich
    pipes break subagent output.

    Does NOTHING except forward the user message to the gateway and
    stream `response.output_text.delta` text back. No allowed_tools,
    no instructions, no context injection, no MEMORY.md, no
    thought_wrapped, no tool rendering, no previous_response_id.

    DEBUG_RAW=true to dump every SSE chunk as a fenced JSON block
    instead of streaming text (useful when subagents still fail —
    you see exactly what the gateway emits).

    If subagents work here but not in chatdragon_responses_*, the
    breaking feature is one of the things this pipe omits.
license: MIT
"""

import json
import logging
from typing import Iterator, Optional

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


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
            description="Dump every SSE chunk as JSON instead of streaming text",
        )

    def __init__(self) -> None:
        self.type = "manifold"
        self.id = "chatdragon_minimal"
        self.name = "chatdragon/"
        self.valves = self.Valves()

    def pipelines(self) -> list[dict]:
        return [{"id": "minimal", "name": "minimal"}]

    def pipe(
        self,
        user_message: str,
        model_id: str,
        messages: list[dict],
        body: dict,
    ) -> Iterator[str]:
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

        try:
            with httpx.stream(
                "POST",
                f"{self.valves.BASE_URL}/v1/responses",
                json=payload,
                timeout=self.valves.TIMEOUT,
                headers={"Accept": "text/event-stream"},
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
                        # Dump everything; truncate giant chunks.
                        payload_str = json.dumps(
                            {"event": event_name, "chunk": chunk},
                            ensure_ascii=False,
                        )
                        if len(payload_str) > 2000:
                            payload_str = payload_str[:2000] + "...(truncated)"
                        yield f"\n```json\n{payload_str}\n```\n"
                        continue

                    # Text delta — the main stream of model output.
                    if chunk_type == "response.output_text.delta":
                        delta = chunk.get("delta", "")
                        if isinstance(delta, str) and delta:
                            yield delta
                        continue

                    # Surface failures inline so we don't fail silently.
                    if chunk_type in ("response.failed", "response.error"):
                        yield f"\n[gateway: {chunk_type}] {json.dumps(chunk, ensure_ascii=False)[:500]}\n"
                        continue
        except httpx.HTTPError as e:
            log.exception("[MINIMAL] gateway call failed")
            yield f"\n[transport error] {e!s}\n"
