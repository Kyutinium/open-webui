"""
title: Oh My Gateway (RAGaaS)
author: claude-code-openai-wrapper
version: 0.3.0-ragaas
description: .
    RAGaaS work-in-progress copy of the Oh My Gateway pipe, kept separate so
    the stable pipe stays untouched while iterating on the ragaas user-identity
    fix (issue #124). Identity is derived from the authenticated __user__
    (server-authoritative) rather than the client-supplied header.

    Oh My Gateway pipe connecting Open WebUI to the oh-my-gateway
    ``/v1/responses`` API. Derived from the stable
    ``chatdragon_responses_wo_userquestions`` variant (no AskUserQuestion
    cards) with two fixes:

    1. Native reasoning passthrough — the gateway emits Claude extended
       thinking as ``response.reasoning_text.delta`` events. Earlier
       variants dropped these, so thinking visible in the gateway admin
       chat never reached Open WebUI. This pipe forwards reasoning wrapped
       in ``<think>...</think>`` tags, which Open WebUI renders as a
       collapsible reasoning panel.

    2. allowed_tools sent only on the first turn — the Claude SDK bakes
       the tool policy at session-create time and has no runtime API to
       swap it, so the gateway rejects ``allowed_tools`` on a continuation
       (``previous_response_id``) turn with a 400
       (``UnsupportedContinuationPolicy``). This pipe sends ``allowed_tools``
       only when starting a new session and skips it on continuations.

    Other features are inherited unchanged:
    - Session-aware via previous_response_id (with task-chain skip
      and 409 stale recovery so multi-turn never ``Stale...``)
    - User context injection (mlm_username from email ID)
    - Credential forwarding for MCP authentication
    - thought_wrapped mode

    Like the variant it derives from, this pipe does NOT surface
    AskUserQuestion / sensitive-file permission prompts as cards.
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

# Regex for parsing the gateway's 409 "Stale previous_response_id" body.  The
# wrapper helpfully includes the latest valid response_id so we can recover
# without forcing a fresh session.  Example body::
#     {"error":{"message":"Stale previous_response_id: only the latest
#      response (resp_<uuid>_<turn>) can be continued","type":"api_error",
#      "code":"409"}}
_STALE_RESP_ID_RE = re.compile(r"\(resp_([0-9a-f-]+)_(\d+)\) can be continued")

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
        MEMORY_REFERENCE_PROMPT: bool = Field(
            default=True,
            description="Inject instruction telling Claude to Read user-level MEMORY.md (/tmp/workspaces/<MLM_USERNAME>/MEMORY.md) first and reference it when planning searches and answering. Shared across backends.",
        )
        MEMORY_UPDATE_PROMPT: bool = Field(
            default=True,
            description="Inject instruction telling Claude to update /tmp/workspaces/<MLM_USERNAME>/MEMORY.md (user-level, NOT pwd-local, NOT .claude/) before each final answer when admission criteria are met. Disable for read-only memory mode.",
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
                "id": "oh-my-gateway-ragaas",
                "name": "Oh My Gateway (RAGaaS)",
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

    def _get_thought_wrapped_instruction(self) -> str:
        # The ``<response>`` token rule is preserved because that's how
        # thought_wrapped mode locates the user-visible portion of the
        # reply.  MEMORY.md *read* and *update* guidance live in their
        # own helpers — see :meth:`_get_memory_reference_instruction`
        # (``MEMORY_REFERENCE_PROMPT`` valve) and
        # :meth:`_get_memory_update_instruction`
        # (``MEMORY_UPDATE_PROMPT`` valve).
        return """

## 답변 작성 규칙

사용자에게 보여줄 최종 답변을 작성하기 직전에 반드시 `<response>` 토큰을 한 번 출력한다.
검색이나 도구 사용 여부와 관계없이 항상 `<response>` 토큰을 출력해야 한다.

- 검색/도구를 사용한 경우: 모든 검색이 끝난 뒤 답변 직전에 `<response>` 출력
- 검색/도구 없이 바로 답변하는 경우: 답변 시작 직전에 `<response>` 출력

이 토큰 이후에 최종 답변을 작성한다."""

    def _get_memory_reference_instruction(self) -> str:
        return """

## MEMORY.md 활용 (시작 시 필수)

MEMORY.md 는 **user-level 파일**이다 — backend (claude / codex / opencode) 와 무관하게 같은 사용자에 묶이므로 per-backend cwd 가 아닌 사용자 디렉토리에 저장된다.

검색이나 답변 작성 전, **`/tmp/workspaces/<MLM_USERNAME>/MEMORY.md` 를 Read** (또는 `../MEMORY.md` 동치) 해서 다음을 확인하고 이번 턴에 반영한다:

**경로 규칙 (반드시 준수)**:
- ✅ **정확**: `/tmp/workspaces/<MLM_USERNAME>/MEMORY.md` — 절대경로, user-level. `<MLM_USERNAME>` 은 시스템 프롬프트의 `<mlm_username>...</mlm_username>` 태그값으로 치환.
- ✅ **동등 표현**: `../MEMORY.md` — pwd 의 parent. 같은 파일.
- ❌ **금지**: `./MEMORY.md` 또는 `<pwd>/MEMORY.md` — 이건 backend-local 위치라 claude / codex / opencode 사이에 분리됨. user-level personalization 의 의도와 어긋남.
- ❌ **금지**: `.claude/MEMORY.md` 또는 `.claude/` 안의 어떤 경로 — Claude Code sensitive-file rule 차단.

확인할 항목:
- 🔵 Procedural — 과거에 효과적이었던 검색 도구 조합 / 쿼리 패턴 / 안티패턴
- 🟡 Semantic — 사용자 선호 (응답 형식, 상세도, 부서 컨텍스트 등)
- 🟢 Episodic — 최근 사용자 컨텍스트 / 진행 중 주제

이 정보를 도구 선택, 쿼리 작성, 답변 스타일에 반영한다. MEMORY.md 가 없거나 비어 있어도 무방 — 그 경우는 새로 축적해 나가면 된다."""

    def _get_memory_update_instruction(self) -> str:
        return """

## MEMORY.md 업데이트 프로토콜 (필수 순서)

답변 작성 직전, 아래 시퀀스를 **이 순서대로** 실행한다:

1. **판단** — 이번 턴에 MEMORY.md 에 추가할 새 entry 가 있는가?
   (Admission 기준: future utility + observation ≥2회 + 기존 항목과 non-duplicate, 모두 Yes 일 때만)

2. **업데이트 필요 시** — 먼저 Edit 도구로 실제 파일 수정:
   `Edit(file_path="/tmp/workspaces/<MLM_USERNAME>/MEMORY.md", old_string="...", new_string="...")`

   **경로 규칙 (반드시 준수)**:
   - ✅ `/tmp/workspaces/<MLM_USERNAME>/MEMORY.md` — 절대경로 (user-level). `<MLM_USERNAME>` 은 `<mlm_username>` 태그값.
   - ✅ `../MEMORY.md` — pwd parent, 동등 표현.
   - ❌ `./MEMORY.md` — backend-local 이라 backend 간 분리. user 메모리 의도와 어긋남.
   - ❌ `.claude/MEMORY.md` 또는 `.claude/` 안의 어떤 경로 — sensitive-file rule 차단.

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

        # Derive identity from the authenticated __user__ (server-injected by
        # Open WebUI), NOT the client-supplied x-openwebui-user-name header — a
        # caller could spoof that header to act as another user downstream. The
        # value flows to the gateway and on to MCP servers (e.g. ragaas) for
        # permission filtering, so it must be server-authoritative. Fall back to
        # the header only when there is no authenticated user context.
        owui_username = ""
        if __user__:
            email = __user__.get("email", "")
            if email and "@" in email:
                owui_username = email.split("@")[0]
            elif email:
                owui_username = email
            if not owui_username:
                owui_username = __user__.get("name", "") or ""
        if not owui_username:
            owui_username = meta_headers.get("x-openwebui-user-name", "")
        if owui_username:
            try:
                owui_username.encode("ascii")
                extra_headers["X-OpenWebUI-User-Name"] = owui_username
            except UnicodeEncodeError:
                from urllib.parse import quote
                extra_headers["X-OpenWebUI-User-Name"] = quote(owui_username)

        # d index resolved by Open WebUI core: prefer the forwarded header, fall
        # back to the user payload. An absent header/None means "not resolved
        # yet"; 0 means "matches no candidate".
        d_index = meta_headers.get("x-openwebui-user-d-index", "")
        if d_index == "" and __user__:
            raw_d_index = __user__.get("d_index")
            d_index = "" if raw_d_index is None else str(raw_d_index)
        if d_index != "":
            extra_headers["X-OpenWebUI-User-D-Index"] = str(d_index)

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
                    if self.valves.MEMORY_REFERENCE_PROMPT and not __task__:
                        content += self._get_memory_reference_instruction()
                    if self.valves.MEMORY_UPDATE_PROMPT and not __task__:
                        content += self._get_memory_update_instruction()
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
                        if self.valves.MEMORY_REFERENCE_PROMPT and not __task__:
                            text += self._get_memory_reference_instruction()
                        if self.valves.MEMORY_UPDATE_PROMPT and not __task__:
                            text += self._get_memory_update_instruction()
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

        # Pass selected MCP tools to gateway as allowed_tools — but ONLY on
        # the first turn of a session. The Claude SDK bakes the tool policy
        # at session-create time and exposes no runtime API to swap it, so
        # the gateway rejects allowed_tools on a continuation turn (one that
        # carries previous_response_id) with a 400
        # (UnsupportedContinuationPolicy). Sending it once is sufficient: the
        # baked policy persists for every continuation in the same chained
        # session.
        is_continuation = "previous_response_id" in payload
        if is_continuation:
            log.info(
                "[PIPE] continuation turn (prev=%s) — skipping allowed_tools "
                "(tool policy is fixed at session start)",
                payload["previous_response_id"],
            )
        else:
            mcp_tools = body.get("mcp_tools") or __metadata__.get("mcp_tools")
            if mcp_tools and isinstance(mcp_tools, list):
                # "Task" lets the orchestrator spawn subagents — required for
                # the subagent grouping UI to have anything to group.
                base_tools = [
                    "Read", "Glob", "Grep", "Bash", "Write", "Edit", "Skill", "Task",
                ]
                payload["allowed_tools"] = base_tools + mcp_tools
                log.info("[PIPE] allowed_tools (new session): %s", payload["allowed_tools"])

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
        # Tracks an open <think> reasoning block in default (non
        # thought_wrapped) mode. The gateway streams Claude extended
        # thinking as response.reasoning_text.delta events; we wrap them in
        # <think>...</think> so Open WebUI renders a reasoning panel.
        reasoning_open = False
        text_buffer = ""
        full_text_acc = ""  # Accumulate full response for image URL detection
        BUFFER_SIZE = 50
        RESPONSE_TAG = "<response>"
        RESPONSE_CLOSE_TAG = "</response>"
        TOOL_DETAILS_PREFIX = "\n\n<details "

        tool_names: dict = {}
        tool_pending: dict = {}
        # Subagent metadata keyed by the Task tool_use_id (== the
        # parent_tool_use_id the SDK stamps on every event a subagent emits).
        # Lets the renderer label and group a subagent's tool calls.
        task_meta: dict = {}
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
                            yield f"\n\nError: {err_msg}"
                            continue

                        # Reasoning (Claude extended thinking) passthrough.
                        # The gateway emits an identical delta on both
                        # response.reasoning_text.delta and
                        # response.reasoning_summary_text.delta — consume only
                        # the raw reasoning_text stream so the text isn't
                        # doubled, and drop the summary variant.
                        if event_type == "response.reasoning_summary_text.delta":
                            continue
                        if event_type == "response.reasoning_text.delta":
                            rdelta = event.get("delta", "")
                            if not rdelta:
                                continue
                            if thought_wrapped:
                                # Already inside an open <thought> block — stream
                                # reasoning straight into it (it IS the thought).
                                # Flush any buffered pre-<response> text first to
                                # preserve ordering.
                                if not response_tag_sent:
                                    if text_buffer:
                                        yield text_buffer
                                        text_buffer = ""
                                    yield rdelta
                            else:
                                if not reasoning_open:
                                    yield "<think>\n"
                                    reasoning_open = True
                                yield rdelta
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
                            # A tool block follows reasoning — close any open
                            # <think> in default mode before emitting it.
                            if reasoning_open and not thought_wrapped:
                                yield "\n</think>\n\n"
                                reasoning_open = False
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
                                    if t_name.startswith("mcp__") and not self._hide_from_explorer(t_name):
                                        results = self._extract_tool_results_for_explorer(raw)
                                        if results:
                                            # Use the friendly label so the
                                            # sidebar shows e.g. "knowledge
                                            # base" rather than the raw MCP
                                            # tool key.
                                            label = self._tool_label(t_name) or t_name
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
                                task_meta,
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
                                # Strip full tags and hold back trailing chars
                                # that could be the start of a tag split across
                                # the next chunk (e.g. "</res" + "ponse>").
                                text_buffer += chunk
                                text_buffer = text_buffer.replace(RESPONSE_CLOSE_TAG, "").replace(RESPONSE_TAG, "")
                                safe_len = len(text_buffer)
                                max_tag_len = max(len(RESPONSE_TAG), len(RESPONSE_CLOSE_TAG))
                                for k in range(max(0, safe_len - max_tag_len + 1), safe_len):
                                    tail = text_buffer[k:]
                                    if RESPONSE_TAG.startswith(tail) or RESPONSE_CLOSE_TAG.startswith(tail):
                                        safe_len = k
                                        break
                                if safe_len > 0:
                                    to_yield = text_buffer[:safe_len]
                                    full_text_acc += to_yield
                                    yield to_yield
                                    text_buffer = text_buffer[safe_len:]
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
                                    text_buffer = ""
                                    if after:
                                        # Re-enter response phase with the
                                        # tail; sanitize close tag and hold
                                        # back any partial-tag prefix.
                                        text_buffer = after.replace(RESPONSE_CLOSE_TAG, "").replace(RESPONSE_TAG, "")
                                        safe_len = len(text_buffer)
                                        max_tag_len = max(len(RESPONSE_TAG), len(RESPONSE_CLOSE_TAG))
                                        for k in range(max(0, safe_len - max_tag_len + 1), safe_len):
                                            tail = text_buffer[k:]
                                            if RESPONSE_TAG.startswith(tail) or RESPONSE_CLOSE_TAG.startswith(tail):
                                                safe_len = k
                                                break
                                        if safe_len > 0:
                                            to_yield = text_buffer[:safe_len]
                                            full_text_acc += to_yield
                                            yield to_yield
                                            text_buffer = text_buffer[safe_len:]
                                elif len(text_buffer) > BUFFER_SIZE:
                                    safe_len = len(text_buffer) - len(RESPONSE_TAG)
                                    if safe_len > 0:
                                        yield text_buffer[:safe_len]
                                        text_buffer = text_buffer[safe_len:]
                        else:
                            # Visible answer text begins — close any open
                            # <think> reasoning block first.
                            if reasoning_open:
                                yield "\n</think>\n\n"
                                reasoning_open = False
                            full_text_acc += chunk
                            yield chunk
                finally:
                    # Close the streaming response we opened via
                    # _open_responses_stream — its context manager isn't
                    # bound to a ``with`` here so we close it manually.
                    resp_cm.__exit__(None, None, None)

        except Exception as e:
            log.error("Stream error: %s", e)
            yield f"\n\nError: {e}"
        finally:
            # Stream ended while a <think> block was still open (reasoning-only
            # turn, or an error before any visible text) — close it so the
            # reasoning panel renders correctly. Only reachable in default
            # mode; thought_wrapped streams reasoning into <thought> instead.
            if reasoning_open:
                yield "\n</think>\n\n"
                reasoning_open = False
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
            elif thought_wrapped and response_tag_sent and text_buffer:
                # Response phase ended with a held-back partial-tag prefix
                # that turned out to be plain text. Strip any complete tags
                # defensively and flush.
                text_buffer = text_buffer.replace(RESPONSE_CLOSE_TAG, "").replace(RESPONSE_TAG, "")
                if text_buffer:
                    full_text_acc += text_buffer
                    yield text_buffer
                text_buffer = ""

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
        task_meta: dict,
    ) -> Optional[str]:
        """Render a system_event into display text (tool blocks, task progress).

        Subagent attribution: every event a subagent emits carries a
        ``parent_tool_use_id`` (the id of the orchestrator's ``Task`` call).
        We tag the subagent's tool-call ``<details>`` blocks with
        ``parent=`` / ``subagent=`` attributes so the frontend can collapse a
        subagent's whole run into one labeled group instead of rendering its
        steps flat alongside the main agent's. The bare ``task_started`` /
        ``task_progress`` / ``task_notification`` lines are dropped — the group
        header conveys the same status, and the subagent's final summary still
        arrives as the ``Task`` tool result.
        """

        if event_type in ("task_started", "task_progress", "task_notification"):
            return None

        elif event_type == "tool_use":
            log.info("[PIPE] tool_use event keys=%s", list(event.keys()))
            tool_id = event.get("tool_use_id", event.get("id", ""))
            name = event.get("name", "")
            parent_id = event.get("parent_tool_use_id")
            if tool_id:
                tool_names[tool_id] = name
            tool_input = event.get("input", event.get("arguments", {}))
            tool_args = json.dumps(tool_input, ensure_ascii=False)
            tool_pending[tool_id] = {
                "name": name,
                "args": tool_args,
                "parent": parent_id,
            }
            # A Task/Agent call spawns a subagent; remember its type/description,
            # keyed by this tool_use_id (== the parent_tool_use_id stamped on
            # everything the subagent emits), so the group can be labeled
            # "type: description". Different gateway versions name the tool
            # "Task" or "Agent".
            if name in ("Task", "Agent") and tool_id and isinstance(tool_input, dict):
                sub_type = tool_input.get("subagent_type") or ""
                sub_desc = tool_input.get("description") or ""
                task_meta[tool_id] = {"type": sub_type, "desc": sub_desc}
            log.info(
                "[PIPE-SUBAGENT] tool_use name=%s id=%s parent=%s keys=%s",
                name, tool_id, parent_id, list(event.keys()),
            )

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
            # Friendly display name (e.g. "knowledge base", "document search
            # MyDB") so the inline "View Result from **NAME**" UI is readable.
            # Falls back to the raw name for tools without a registered label.
            display_name = self._tool_label(name) or name
            esc_name = html.escape(display_name)

            if self.valves.MCP_TOOL_ONLY and not name.startswith("mcp__"):
                return None

            # Group attribution: a subagent's child tool calls carry the
            # parent Task's id; the Task's own result joins that same group
            # (keyed by its own id) so the subagent header collects its steps
            # *and* its final summary. ``group_id`` empty -> a normal
            # main-agent tool call, rendered ungrouped as before.
            # Read parent from the matched tool_use (pending) OR from the
            # tool_result event itself — some gateways stamp parent_tool_use_id
            # only on the result, or emit no separate tool_use event at all.
            parent_id = pending.get("parent") or event.get("parent_tool_use_id")
            group_id = parent_id or (tool_id if name in ("Task", "Agent") else "")
            subagent_attrs = ""
            if group_id:
                meta = task_meta.get(group_id, {})
                label = ": ".join(
                    p for p in (meta.get("type", ""), meta.get("desc", "")) if p
                ) or "subagent"
                subagent_attrs = (
                    f' parent="{_safe_attr(str(group_id))}"'
                    f' subagent="{_safe_attr(label)}"'
                )
            log.info(
                "[PIPE-SUBAGENT] tool_result name=%s id=%s pending_parent=%s "
                "event_parent=%s group=%s grouped=%s",
                name, tool_id, pending.get("parent"),
                event.get("parent_tool_use_id"), group_id, bool(subagent_attrs),
            )

            if not self.valves.TOOL_DISPLAY:
                friendly = self._friendly_tool_notification(name, is_error)
                details_tag = f"\n> {friendly}\n"
            else:
                safe_args = _safe_attr(args)
                safe_result = _safe_attr(result_content)
                details_tag = (
                    f'\n\n<details type="tool_calls"'
                    f' name="{esc_name}"'
                    f"{subagent_attrs}"
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

    # MCP server-prefix rules. Used when the tool key is dynamic (e.g. a
    # per-DB suffix) and a static suffix→label map can't enumerate them.
    # ``{tool}`` is replaced with everything after the server segment.
    _MCP_SERVER_LABELS: dict[str, str] = {
        "doc_retrieval": "document search {tool}",
    }

    # Tools whose results are too low-signal to surface in the right-sidebar
    # Tool Explorer (e.g. glossary / common-knowledge lookups users already
    # know).  Inline ``<details type="tool_calls">`` blocks are still
    # rendered — only the explorer aggregation is suppressed.
    _TOOL_EXPLORER_HIDE: set[str] = {"basic_knowledge"}

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
    def _hide_from_explorer(cls, raw_name: str) -> bool:
        """Return True if a tool's results should be omitted from the
        right-sidebar Tool Explorer (matches both ``mcp__<server>__…`` and
        OpenCode-flattened ``<server>_…`` forms)."""
        lower = raw_name.lower()
        if lower.startswith("mcp__"):
            parts = lower.split("__")
            if len(parts) >= 2 and parts[1] in cls._TOOL_EXPLORER_HIDE:
                return True
        tokens = lower.split("_")
        for hide_key in cls._TOOL_EXPLORER_HIDE:
            key_tokens = hide_key.split("_")
            n = len(key_tokens)
            if len(tokens) >= n and tokens[:n] == key_tokens:
                return True
        return False

    @classmethod
    def _tool_label(cls, raw_name: str) -> str:
        """Return a short, human-friendly label for a tool name.

        Resolution order for ``mcp__<server>__<tool>`` names:
          1. Server-prefix template in ``_MCP_SERVER_LABELS`` (dynamic tools
             like ``mcp__doc_retrieval__<dbname>`` → ``document search <dbname>``).
          2. Exact tool-key match in ``_MCP_LABELS``.
          3. Tool key with underscores replaced by spaces.
        """
        lower = raw_name.lower()
        if lower in cls._BUILTIN_LABELS:
            return cls._BUILTIN_LABELS[lower]
        if lower.startswith("mcp__"):
            parts = raw_name.split("__")
            if len(parts) >= 3:
                server = parts[1].lower()
                tool = "__".join(parts[2:])
                if server in cls._MCP_SERVER_LABELS:
                    return cls._MCP_SERVER_LABELS[server].format(tool=tool)
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
                # Save response ID — skip for task requests so task
                # responses don't displace the chat's chained response
                # and trigger a 409 on the next user turn (see _stream
                # for the full reasoning).
                resp_id = data.get("id", "")
                if resp_id and chat_id and not task:
                    self._response_ids[chat_id] = resp_id

                # Extract text from output items, in emission order so
                # interleaved reasoning/message items reconstruct correctly.
                # In default mode, reasoning items are wrapped in <think> tags
                # for Open WebUI's reasoning panel. In thought_wrapped mode we
                # skip the <think> wrapping and let _wrap_thought_content
                # handle the <thought> framing below.
                output = data.get("output", [])
                thought_wrapped = (
                    self.valves.OUTPUT_FORMAT == "thought_wrapped" and not task
                )
                content = ""
                for item in output:
                    item_type = item.get("type")
                    if item_type == "message":
                        for part in item.get("content", []):
                            if part.get("type") == "output_text":
                                content += part.get("text", "")
                    elif item_type == "reasoning" and not thought_wrapped:
                        reasoning_text = "".join(
                            part.get("text", "")
                            for part in (item.get("content") or [])
                            if part.get("type") == "reasoning_text"
                        )
                        if reasoning_text.strip():
                            content += f"<think>\n{reasoning_text.strip()}\n</think>\n\n"

                if thought_wrapped:
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
