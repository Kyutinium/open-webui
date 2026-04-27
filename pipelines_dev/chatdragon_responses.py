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
        # Track pending AskUserQuestion function_calls per chat. When the
        # gateway returns response.completed with status="requires_action",
        # the user's next message must be routed back as function_call_output
        # (matched by call_id) instead of a regular input. Without this, the
        # SDK 0.1.57+ permission prompts (e.g. MEMORY.md sensitive write) get
        # surfaced but the turn never closes.
        self._pending_function_calls: dict[str, dict] = {}

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

이 토큰 이후에 최종 답변을 작성한다.

## MEMORY.md 업데이트 프로토콜 (필수 순서)

답변 작성 직전, 아래 시퀀스를 **이 순서대로** 실행한다:

1. **판단** — 이번 턴에 MEMORY.md 에 추가할 새 entry 가 있는가?
   (Admission 기준: future utility + observation ≥2회 + 기존 항목과 non-duplicate, 모두 Yes 일 때만)

2. **업데이트 필요 시** — 먼저 Edit 도구로 실제 파일 수정:
   `Edit(file_path=".claude/MEMORY.md", old_string="...", new_string="...")`

3. Edit 도구의 성공 반환 (예: "File updated") 을 **확인한 직후**, 마커 출력:
   `MEMORY_UPDATE: <방금 추가한 entry 한 줄 요약>`

4. **업데이트 불필요 시**: `MEMORY_SKIP: <사유>` 출력
   (사유 예시: "novelty 미달" / "observation <2회" / "기존 항목과 중복" / "update 불필요")

5. 마지막으로 `<response>` 토큰 출력.

**금지 규칙**: Edit 도구 호출이 선행되지 않았다면 `MEMORY_UPDATE` 를 적지 마라.
Edit 없이 `MEMORY_UPDATE` 를 출력하는 것은 **false reporting** 이며 protocol violation 이다.

출력 예 (업데이트 수행 시):
```
[Edit 도구 호출 → "File updated" 결과 확인됨]
MEMORY_UPDATE: mm_cql 제품명+속성 키워드 패턴 3회차 관찰
<response>
```

이 마커 라인은 `<thought>` collapsible 안에 남고 최종 사용자 응답에는 표시되지 않는다."""

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
        """Build a <details type='tool_explorer'> tag with JSON body.

        *tool_data* is a dict keyed by tool label, each value being a list
        of call dicts with ``query`` and ``results`` keys.
        """
        body = json.dumps(tool_data, ensure_ascii=False)
        return (
            f'\n\n<details type="tool_explorer" done="true">\n'
            f'<summary>Tool Results</summary>\n'
            f'{body}\n'
            f'</details>\n\n'
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

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
                    if (
                        self.valves.OUTPUT_FORMAT == "thought_wrapped"
                        and self.valves.THOUGHT_WRAPPED_INSTRUCTION
                        and not __task__
                    ):
                        content += self._get_thought_wrapped_instruction()
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
                        if (
                            self.valves.OUTPUT_FORMAT == "thought_wrapped"
                            and self.valves.THOUGHT_WRAPPED_INSTRUCTION
                            and not __task__
                        ):
                            text += self._get_thought_wrapped_instruction()
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

        # If the previous turn ended in requires_action (AskUserQuestion or
        # SDK permission prompt), the wrapper expects a function_call_output
        # matched by call_id — not a fresh user message. Routing as a normal
        # input here would either error or leave the function call dangling.
        pending_fc = (
            self._pending_function_calls.pop(chat_id, None) if chat_id else None
        )
        prev_resp_id = self._response_ids.get(chat_id) if chat_id else None

        if pending_fc:
            payload = {
                "model": self.valves.MODEL,
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": pending_fc.get("call_id", ""),
                        "output": last_user_content if isinstance(last_user_content, str)
                        else json.dumps(last_user_content, ensure_ascii=False),
                    }
                ],
                "stream": use_stream,
            }
            if prev_resp_id:
                payload["previous_response_id"] = prev_resp_id
            log.info(
                "[PIPE] resuming function_call name=%s call_id=%s for chat=%s",
                pending_fc.get("name", ""),
                pending_fc.get("call_id", ""),
                chat_id,
            )
        else:
            payload = {
                "model": self.valves.MODEL,
                "input": last_user_content,
                "stream": use_stream,
            }

            # Multi-turn: use previous_response_id for continuity
            if prev_resp_id:
                payload["previous_response_id"] = prev_resp_id
            else:
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
        thought_wrapped = self.valves.OUTPUT_FORMAT == "thought_wrapped" and not task
        thought_opened = False
        response_tag_sent = False
        text_buffer = ""
        full_text_acc = ""  # Accumulate full response for image URL detection
        BUFFER_SIZE = 50
        RESPONSE_TAG = "<response>"
        RESPONSE_CLOSE_TAG = "</response>"
        TOOL_DETAILS_PREFIX = "\n\n<details "

        tool_names: dict = {}
        tool_pending: dict = {}
        any_tool_used = False
        collected_thumbnails: list[str] = []  # Thumbnails from MCP tool results
        # Tool explorer: {tool_label: [{query, results}]}
        tool_explorer_data: dict[str, list[dict]] = {}
        try:
            if thought_wrapped:
                yield "<thought>\n"
                thought_opened = True

            url = f"{self.valves.BASE_URL.rstrip('/')}/v1/responses"
            timeout = httpx.Timeout(
                connect=30.0,
                read=float(self.valves.TIMEOUT),
                write=30.0,
                pool=30.0,
            )
            with httpx.Client(timeout=timeout) as client:
                with client.stream("POST", url, json=payload, headers=self._make_headers()) as resp:
                    if resp.status_code != 200:
                        body_text = resp.read().decode()
                        raise Exception(f"Server error ({resp.status_code}): {body_text}")

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

                        # Save response ID for multi-turn continuity
                        if event_type == "response.completed":
                            resp_obj = event.get("response", {})
                            resp_id = resp_obj.get("id", "")
                            if resp_id and chat_id:
                                self._response_ids[chat_id] = resp_id
                                log.info("[PIPE] saved response_id=%s for chat=%s", resp_id, chat_id)

                            # Detect AskUserQuestion (requires_action). The
                            # gateway/SDK surfaces permission prompts and
                            # explicit AskUserQuestion calls as a function_call
                            # output item with status="requires_action". The
                            # user's next message is routed as function_call_output
                            # in pipe() so the wrapper turn can close.
                            if resp_obj.get("status") == "requires_action":
                                fc_item = None
                                for item in resp_obj.get("output", []):
                                    if (
                                        isinstance(item, dict)
                                        and item.get("type") == "function_call"
                                    ):
                                        fc_item = item
                                        break
                                if fc_item and chat_id:
                                    self._pending_function_calls[chat_id] = {
                                        "call_id": fc_item.get("call_id", ""),
                                        "name": fc_item.get("name", ""),
                                        "arguments": fc_item.get("arguments", "{}"),
                                    }
                                    log.info(
                                        "[PIPE] pending function_call name=%s call_id=%s for chat=%s",
                                        fc_item.get("name", ""),
                                        fc_item.get("call_id", ""),
                                        chat_id,
                                    )
                                    rendered = self._render_ask_user_question(fc_item)
                                    if rendered:
                                        if thought_wrapped and not response_tag_sent:
                                            if text_buffer:
                                                yield text_buffer
                                                text_buffer = ""
                                            yield "\n</thought>\n\n"
                                            response_tag_sent = True
                                        yield rendered
                            continue

                        if event_type == "response.failed":
                            err = event.get("response", {}).get("error", {})
                            err_msg = err.get("message", "Unknown error")
                            log.error("[PIPE] response.failed: %s", err_msg)
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
                                if is_persisted and t_name.startswith("mcp__"):
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
                                    if t_name == "Read" or not t_name.startswith("mcp__"):
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

                                    parsed = self._parse_tool_content(raw)
                                    log.info(
                                        "[PIPE-PARSE] parsed type=%s result=%s",
                                        type(parsed).__name__ if parsed else "None",
                                        str(parsed)[:300] if parsed else "None",
                                    )
                                    # Thumbnails for gallery
                                    thumbs = self._extract_thumbnails_from_tool_result(raw)
                                    if thumbs:
                                        collected_thumbnails.extend(thumbs)
                                        log.info("[PIPE] collected %d thumbnails", len(thumbs))
                                    # Structured results for tool explorer
                                    if t_name.startswith("mcp__"):
                                        results = self._extract_tool_results_for_explorer(raw)
                                        if results:
                                            parts = t_name.split("__")
                                            label = parts[1] if len(parts) >= 2 else t_name
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
                                            explorer_tag = self._build_tool_explorer_tag(
                                                {label: [call_data]}
                                            )
                                            if thought_wrapped and not response_tag_sent:
                                                if text_buffer:
                                                    yield text_buffer
                                                    text_buffer = ""
                                                yield explorer_tag
                                            else:
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
                                if thought_wrapped and not response_tag_sent:
                                    # Tool <details> blocks bypass the buffer
                                    if text_buffer:
                                        yield text_buffer
                                        text_buffer = ""
                                    yield rendered
                                else:
                                    yield rendered
                            continue

                        # Text delta handling
                        if event_type != "response.output_text.delta":
                            continue
                        chunk = event.get("delta", "")
                        if not chunk:
                            continue

                        # Filter SDK tool-execution noise
                        stripped = chunk.strip()
                        if _is_tool_noise(stripped):
                            continue

                        if thought_wrapped:
                            if response_tag_sent:
                                chunk = chunk.replace(RESPONSE_CLOSE_TAG, "").replace(RESPONSE_TAG, "")
                                if chunk:
                                    full_text_acc += chunk
                                    yield chunk
                            elif chunk.startswith(TOOL_DETAILS_PREFIX):
                                # Tool <details> blocks bypass the buffer
                                if text_buffer:
                                    yield text_buffer
                                    text_buffer = ""
                                yield chunk
                            else:
                                text_buffer += chunk
                                if RESPONSE_TAG in text_buffer:
                                    idx = text_buffer.index(RESPONSE_TAG)
                                    before = text_buffer[:idx]
                                    after = text_buffer[idx + len(RESPONSE_TAG):]
                                    if before:
                                        yield before
                                    yield "\n</thought>\n\n"
                                    response_tag_sent = True
                                    if after:
                                        full_text_acc += after
                                        yield after
                                    text_buffer = ""
                                elif len(text_buffer) > BUFFER_SIZE:
                                    safe_len = len(text_buffer) - len(RESPONSE_TAG)
                                    if safe_len > 0:
                                        yield text_buffer[:safe_len]
                                        text_buffer = text_buffer[safe_len:]
                        else:
                            full_text_acc += chunk
                            yield chunk

        except Exception as e:
            log.error("Stream error: %s", e)
            yield f"\n\nError: {e}"
        finally:
            if thought_wrapped and thought_opened and not response_tag_sent:
                if text_buffer:
                    text_buffer = text_buffer.replace(RESPONSE_CLOSE_TAG, "")
                if not any_tool_used and text_buffer:
                    # No tools were used and model didn't emit <response> —
                    # treat the entire content as the response, not thought.
                    yield "\n</thought>\n\n"
                    full_text_acc += text_buffer
                    yield text_buffer
                else:
                    if text_buffer:
                        full_text_acc += text_buffer
                        yield text_buffer
                    yield "\n</thought>"

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

    def _render_ask_user_question(self, fc_item: dict) -> str:
        """Render an AskUserQuestion function_call for Open WebUI.

        Emits a ``<details type="ask_user_question">`` block. The Svelte
        token dispatcher (``MarkdownTokens.svelte``) intercepts this and
        renders an interactive ``AskUserQuestionCard`` with clickable
        options. Falls back to readable markdown if the body fails to
        parse on the frontend.

        The body JSON shape mirrors a2a-agent's ``pendingQuestion``::

          {"callId": "...",
           "name": "AskUserQuestion",
           "questions": [{"question": "...",
                          "options": [{"label": "...", "description": "..."}],
                          "multiSelect": false}]}
        """
        name = fc_item.get("name", "AskUserQuestion")
        call_id = fc_item.get("call_id", "")
        try:
            args = json.loads(fc_item.get("arguments", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {}

        questions_list = args.get("questions")
        if isinstance(questions_list, list) and questions_list:
            raw_items = questions_list
        else:
            raw_items = [args]

        normalized: list[dict] = []
        for q in raw_items:
            if not isinstance(q, dict):
                continue
            question_text = q.get("question") or q.get("prompt") or ""
            options_raw = q.get("options")
            options: list[dict] = []
            if isinstance(options_raw, list):
                for opt in options_raw:
                    if isinstance(opt, dict):
                        label = opt.get("label", "")
                        desc = opt.get("description", "")
                    else:
                        label = str(opt)
                        desc = ""
                    if label:
                        options.append({"label": label, "description": desc})
            normalized.append({
                "question": question_text,
                "options": options,
                "multiSelect": bool(q.get("multiSelect")),
            })

        # Drop completely empty entries (no question text and no options) —
        # except when they are the only item (permission prompts may carry
        # the payload under unknown keys; we surface the raw args then).
        if any(q.get("question") or q.get("options") for q in normalized):
            normalized = [
                q for q in normalized
                if q.get("question") or q.get("options")
            ]

        body = {
            "callId": call_id,
            "name": name,
            "questions": normalized,
            "raw": args if not normalized or not any(
                q.get("question") for q in normalized
            ) else None,
        }
        # Drop None values for a clean payload
        body = {k: v for k, v in body.items() if v is not None}

        body_json = json.dumps(body, ensure_ascii=False)

        if name == "AskUserQuestion":
            summary = "❓ 추가 입력이 필요합니다"
        else:
            summary = f"❓ 권한/입력 요청: {name}"

        # Wrap in <details> so Open WebUI's MarkdownTokens.svelte can
        # dispatch on attributes.type. The body is JSON; the Svelte side
        # strips <summary> and JSON.parse()s the rest. ``done="true"`` keeps
        # the card interactive once the message stream finishes.
        return (
            "\n\n"
            f'<details type="ask_user_question" done="true">\n'
            f"<summary>{summary}</summary>\n"
            f"{body_json}\n"
            "</details>\n\n"
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
            result_content = self._extract_tool_result_text(raw_content)
            if not result_content and is_error:
                result_content = event.get("error", "Tool execution failed")
            # SDK overflow: shorten the verbose message.
            if result_content.startswith("Error: result ("):
                m = re.search(r"\(([0-9,]+) characters?\)", result_content)
                chars = m.group(1) if m else "large"
                result_content = f"Result truncated ({chars} chars)"
            result_content = result_content[:10000]
            esc_name = html.escape(name)

            if self.valves.MCP_TOOL_ONLY and not name.startswith("mcp__"):
                return None

            if not self.valves.TOOL_DISPLAY:
                friendly = self._friendly_tool_notification(name, is_error)
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

    # ── Friendly tool notification helpers ──────────────────────────────
    # Maps raw MCP tool-name suffix → friendly display name.
    _MCP_LABELS: dict[str, str] = {
        "mlm_cql": "MLM Confluence",
        "cql": "Confluence",
        "basic_knowledge": "knowledge base",
        "jira_search": "Jira",
        "jira_issue": "Jira issue",
        "web_search": "the web",
        "slack_search": "Slack",
        "google_drive": "Google Drive",
    }

    # Built-in SDK tools → friendly display name.
    _BUILTIN_LABELS: dict[str, str] = {
        "read": "a file",
        "edit": "a file",
        "write": "a file",
        "bash": "a command",
        "grep": "the codebase",
        "glob": "files",
        "todowrite": "the task list",
        "webfetch": "a webpage",
        "websearch": "the web",
        "notebookedit": "a notebook",
    }

    # Completion templates – "{label}" is replaced with the tool's display name.
    _DONE_TEMPLATES: list[str] = [
        "Finished searching {label}",
        "Done looking through {label}",
        "Completed {label} search",
        "Searched {label} successfully",
        "Got results from {label}",
        "Pulled data from {label}",
        "Wrapped up {label} lookup",
        "{label} search complete",
        "Retrieved results from {label}",
        "All done with {label}",
    ]

    _ERROR_TEMPLATES: list[str] = [
        "Failed to search {label}",
        "Something went wrong with {label}",
        "Could not complete {label} search",
    ]

    @classmethod
    def _tool_label(cls, raw_name: str) -> str:
        """Return a short, human-friendly label for a tool name."""
        lower = raw_name.lower()
        if lower in cls._BUILTIN_LABELS:
            return cls._BUILTIN_LABELS[lower]
        if lower.startswith("mcp__"):
            parts = raw_name.split("__")
            tool_key = parts[-1] if len(parts) >= 3 else parts[-1]
            if tool_key.lower() in cls._MCP_LABELS:
                return cls._MCP_LABELS[tool_key.lower()]
            return tool_key.replace("_", " ")
        return raw_name

    @classmethod
    def _friendly_tool_notification(cls, raw_name: str, is_error: bool = False) -> str:
        """Build a single-tool notification (fallback when buffer is unavailable)."""
        label = cls._tool_label(raw_name)
        if is_error:
            template = random.choice(cls._ERROR_TEMPLATES)
            return f"❌ {template.format(label=label)}"
        template = random.choice(cls._DONE_TEMPLATES)
        return f"✅ {template.format(label=label)}"

    @staticmethod
    def _extract_tool_result_text(raw_content) -> str:
        """Extract plain text from tool result content.

        Content may be a string, a list of text-block dicts, or a JSON-serialized
        version of either.  This method normalises all variants into a single
        plain-text string so the result can be safely placed in an HTML attribute.
        """
        if not raw_content:
            return ""

        # List of content blocks: [{"type": "text", "text": "..."}]
        if isinstance(raw_content, list):
            parts = []
            for b in raw_content:
                if isinstance(b, dict):
                    parts.append(b.get("text", ""))
                else:
                    parts.append(str(b))
            return " ".join(parts).strip()

        text = str(raw_content).strip()

        # If the string looks like a JSON array of text blocks, parse it
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    parts = []
                    for b in parsed:
                        if isinstance(b, dict):
                            parts.append(b.get("text", ""))
                        else:
                            parts.append(str(b))
                    return " ".join(parts).strip()
            except (json.JSONDecodeError, TypeError):
                pass

        return text

    # ------------------------------------------------------------------
    # Non-streaming
    # ------------------------------------------------------------------

    def _non_stream(self, payload: dict, task: Optional[str], chat_id: str = "") -> str:
        url = f"{self.valves.BASE_URL.rstrip('/')}/v1/responses"
        payload["stream"] = False
        try:
            with httpx.Client(timeout=httpx.Timeout(self.valves.TIMEOUT)) as client:
                resp = client.post(url, json=payload, headers=self._make_headers())
                if resp.status_code != 200:
                    return f"Error: Server error ({resp.status_code}): {resp.text}"

                data = resp.json()
                # Save response ID
                resp_id = data.get("id", "")
                if resp_id and chat_id:
                    self._response_ids[chat_id] = resp_id

                # Extract text from output items
                output = data.get("output", [])
                content = ""
                for item in output:
                    if item.get("type") == "message":
                        for part in item.get("content", []):
                            if part.get("type") == "output_text":
                                content += part.get("text", "")

                if self.valves.OUTPUT_FORMAT == "thought_wrapped" and not task:
                    content = self._wrap_thought_content(content)
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
