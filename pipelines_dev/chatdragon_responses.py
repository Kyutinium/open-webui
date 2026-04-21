"""
title: Development Assistant (Responses)
author: claude-code-openai-wrapper
version: 0.2.0
description: .
    Session-aware responses pipe via /v1/responses.
    Uses previous_response_id for conversation continuity.
    Features:
    - User context injection (mlm_username from email ID)
    - Credential forwarding from Open WebUI middleware for MCP authentication
    - thought_wrapped mode: wraps thinking in <thought> tags and detects <response> tag
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

# Regex to detect SDK tool-execution noise that leaks into text deltas:
#   - Bare tool names like "mcp__mcp_router__cql", "Read", "Bash"
#   - "Executing tool_name..." status lines
_TOOL_NOISE_RE = re.compile(
    r"^(?:Executing\s+)?(?:mcp__\w+|Read|Bash|Write|Edit|Glob|Grep|WebFetch|WebSearch|"
    r"NotebookEdit|Agent|TodoWrite|Skill)(?:\.\.\.)?\s*$"
)


def _is_tool_noise(text: str) -> bool:
    """Return True if *text* is SDK tool-execution noise."""
    return bool(text) and _TOOL_NOISE_RE.match(text) is not None


def _safe_attr(value: str) -> str:
    """Sanitize a string for use inside a double-quoted HTML attribute.

    Open WebUI reads raw attribute values without decoding HTML entities,
    so we use plain character substitution instead of entity encoding.
    ``&`` is neutralised so pre-existing entities in Confluence content
    (e.g. ``&quot;``) cannot be decoded by the browser into ``"`` which
    would break the attribute boundary.
    """
    return (
        value
        .replace("&", "+")   # neutralise entities (must be first)
        .replace('"', "'")   # prevent closing the attribute
        .replace("<", "[")
        .replace(">", "]")
        .replace("\n", " ")
        .replace("\r", "")
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
            default="sonnet",
            description="Claude model to use (e.g. sonnet, opus, haiku)",
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
        # Thought wrapped mode settings
        OUTPUT_FORMAT: str = Field(
            default="default",
            description="Output format: 'default' (stream as-is) or 'thought_wrapped' (wrap thinking in <thought> tags)",
        )
        THOUGHT_WRAPPED_INSTRUCTION: bool = Field(
            default=True,
            description="Inject instruction for model to output <response> tag when done thinking",
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
                "id": "chatdragon-responses",
                "name": "Chatdragon Responses",
            }
        ]

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

    def _get_thought_wrapped_instruction(self) -> str:
        return """

## 답변 작성 규칙

사용자에게 보여줄 최종 답변을 작성하기 직전에 반드시 `<response>` 토큰을 한 번 출력한다.
검색이나 도구 사용 여부와 관계없이 항상 `<response>` 토큰을 출력해야 한다.

- 검색/도구를 사용한 경우: 모든 검색이 끝난 뒤 답변 직전에 `<response>` 출력
- 검색/도구 없이 바로 답변하는 경우: 답변 시작 직전에 `<response>` 출력

이 토큰 이후에 최종 답변을 작성한다."""

    def _wrap_thought_content(self, text: str) -> str:
        if not text:
            return text
        if "<response>" in text:
            parts = text.split("<response>", 1)
            thought_content = parts[0].strip()
            response_content = parts[1].replace("<response>", "").replace("</response>", "").strip() if len(parts) > 1 else ""
            return f"<thought>\n{thought_content}\n</thought>\n\n{response_content}"
        return f"<thought>\n{text}\n</thought>"

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

    @staticmethod
    def _parse_tool_content(raw_content):
        """Normalise raw MCP tool result into a Python object.

        Handles: direct dict/list, JSON string, content-block list
        ``[{type: text, text: ...}]``, and Python-repr single-quote strings.
        Returns the parsed object or ``None`` on failure.
        """
        if not raw_content:
            return None

        data = raw_content

        # Content-block list: [{"type": "text", "text": "..."}]
        if isinstance(data, list):
            texts = []
            for b in data:
                if isinstance(b, dict) and b.get("type") == "text":
                    texts.append(b.get("text", ""))
                elif isinstance(b, str):
                    texts.append(b)
            if texts:
                data = " ".join(texts).strip()
            else:
                # Already a plain list of results
                return data

        if isinstance(data, dict):
            return data

        if not isinstance(data, str):
            return None

        text = data.strip()

        # Strip line-number prefixes from Read tool output (cat -n format):
        # "1\t[\n2\t  {\n" or "1       [\n2         {\n" (tabs or spaces)
        if re.match(r"^\d+[\t ]", text):
            log.info(
                "[PIPE-PARSE] pre-strip: len=%d newlines=%d first200=%s",
                len(text), text.count("\n"), repr(text[:200]),
            )
            lines = text.split("\n")
            stripped = []
            for line in lines:
                m = re.match(r"^\d+[\t ]+(.*)", line)
                stripped.append(m.group(1) if m else line)
            text = "\n".join(stripped).strip()
            log.info(
                "[PIPE-PARSE] after line-strip: len=%d lines=%d first200=%s",
                len(text), len(stripped), repr(text[:200]),
            )

        # Try standard JSON first, then Python literal
        parsed = None
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as e:
            # "Extra data" means valid JSON followed by trailing content;
            # truncate at the reported position and retry.
            if "Extra data" in str(e) and hasattr(e, "pos") and e.pos:
                try:
                    parsed = json.loads(text[:e.pos])
                    log.info("[PIPE-PARSE] json.loads recovered by truncating at pos=%d", e.pos)
                except (json.JSONDecodeError, ValueError):
                    pass
            if parsed is None:
                log.info("[PIPE-PARSE] json.loads failed: %s", str(e)[:200])
                import ast
                try:
                    parsed = ast.literal_eval(text)
                except (ValueError, SyntaxError) as e2:
                    log.info("[PIPE-PARSE] ast.literal_eval failed: %s", str(e2)[:200])
                    return None

        if parsed is None:
            return None

        # If result is a content-block list, extract text and re-parse
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and parsed[0].get("type") == "text":
            inner_texts = []
            for b in parsed:
                if isinstance(b, dict) and b.get("type") == "text":
                    inner_texts.append(b.get("text", ""))
            inner = " ".join(inner_texts).strip()
            if inner:
                try:
                    return json.loads(inner)
                except (json.JSONDecodeError, ValueError):
                    import ast
                    try:
                        return ast.literal_eval(inner)
                    except (ValueError, SyntaxError):
                        pass
            return None

        return parsed

    @staticmethod
    def _extract_thumbnails_from_tool_result(raw_content) -> list[str]:
        """Extract thumbnail URLs from MCP tool result content."""
        data = Pipeline._parse_tool_content(raw_content)
        if not data:
            return []

        thumbnails: list[str] = []

        def _collect(items):
            if not isinstance(items, list):
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                meta = item.get("metadata") or {}
                thumb = (
                    item.get("thumbnail") or item.get("thumbnail_url") or ""
                    or meta.get("thumbnail") or meta.get("thumbnail_url") or ""
                )
                if thumb and isinstance(thumb, str):
                    thumbnails.append(thumb)

        if isinstance(data, dict):
            for key in ("responses", "results", "data", "items"):
                if isinstance(data.get(key), list):
                    _collect(data[key])
                    break
        elif isinstance(data, list):
            _collect(data)

        return thumbnails

    @staticmethod
    def _extract_tool_results_for_explorer(raw_content) -> list[dict]:
        """Extract structured results from MCP tool result for the explorer sidebar."""
        data = Pipeline._parse_tool_content(raw_content)
        if not data:
            return []

        items_list = None
        if isinstance(data, dict):
            for key in ("responses", "results", "data", "items"):
                if isinstance(data.get(key), list):
                    items_list = data[key]
                    break
        elif isinstance(data, list):
            items_list = data

        if not items_list:
            return []

        results = []
        for item in items_list:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            # Skip error results
            if meta.get("error") or (
                item.get("content", "").startswith("오류 발생")
                or item.get("content", "").lower().startswith("error")
            ):
                continue
            # URL: try multiple field names and Confluence _links
            url = (
                meta.get("url") or meta.get("edm_link")
                or item.get("url") or item.get("edm_link") or ""
            )
            if not url:
                # Confluence: build URL from _links.webui or page id
                links = meta.get("_links") or item.get("_links") or {}
                if links.get("webui"):
                    # Try to get base from space self link
                    space = meta.get("space") or {}
                    space_links = space.get("_links") or {}
                    base = ""
                    if space_links.get("self"):
                        # e.g. https://confluence.example.com/rest/api/space/KEY
                        base = space_links["self"].split("/rest/")[0]
                    if base:
                        url = f"{base}{links['webui']}"
                    else:
                        url = links["webui"]
                elif meta.get("page_id") or meta.get("id"):
                    page_id = meta.get("page_id") or meta.get("id")
                    space = meta.get("space") or {}
                    space_links = space.get("_links") or {}
                    if space_links.get("self"):
                        base = space_links["self"].split("/rest/")[0]
                        url = f"{base}/pages/viewpage.action?pageId={page_id}"
            # Thumbnail
            thumbnail = (
                meta.get("thumbnail") or meta.get("thumbnail_url")
                or item.get("thumbnail") or item.get("thumbnail_url") or ""
            )
            results.append({
                "title": item.get("title", ""),
                "content": (item.get("content") or "")[:200],
                "url": url,
                "thumbnail": thumbnail,
                "doc_type": item.get("doc_type") or meta.get("type") or "",
            })
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

    @staticmethod
    def _build_tool_explorer_tag(tool_data: dict) -> str:
        """Build a <details type='tool_exp