"""
Helpers for ``chatdragon_responses_opencode``.

Pure-data and pure-function module split out of the main pipe to keep
each pushed file small.  Contains: HTML attribute sanitiser, MCP tool
result parsing, tool-explorer / gallery tag builders, MCP tool-name
detection, friendly tool labels.

No Pipeline state lives here — every symbol is module-level.
"""

from __future__ import annotations

import ast
import html
import json
import logging
import random
import re

log = logging.getLogger(__name__)

# 409 stale-response-id regex used by the main pipe to recover from a
# stale ``previous_response_id`` chain on the gateway.  Example body::
#     {"error":{"message":"Stale previous_response_id: only the latest
#      response (resp_<uuid>_<turn>) can be continued","type":"api_error",
#      "code":"409"}}
STALE_RESP_ID_RE = re.compile(r"\(resp_([0-9a-f-]+)_(\d+)\) can be continued")


def safe_attr(value: str) -> str:
    """Sanitize a string for use inside a double-quoted HTML attribute.

    Open WebUI reads raw attribute values without decoding HTML
    entities, so we use plain character substitution instead of
    entity encoding.  ``&`` is neutralised first so pre-existing
    entities in Confluence content (e.g. ``&quot;``) cannot be
    decoded by the browser into ``"`` which would break the
    attribute boundary.
    """
    return (
        value
        .replace("&", "+")
        .replace('"', "'")
        .replace("<", "[")
        .replace(">", "]")
        .replace("\n", " ")
        .replace("\r", "")
    )


# ── MCP tool-name detection ───────────────────────────────────────────
# Two naming conventions land here:
#   - Claude SDK:     mcp__<server>__<tool>   (double underscore)
#   - opencode native:<server>:<tool>          (colon — see
#                                               packages/opencode/src/mcp/index.ts
#                                               sanitize() logic)
# Built-in opencode tools (Read, Bash, Edit, …) have neither separator.

def is_mcp_tool(raw_name: str) -> bool:
    """True if *raw_name* is an MCP tool under either convention."""
    if not raw_name:
        return False
    if raw_name.startswith("mcp__"):
        return True
    if ":" in raw_name:
        left, _, right = raw_name.partition(":")
        if left and right and "/" not in raw_name and " " not in raw_name:
            return True
    return False


def mcp_label_key(raw_name: str) -> str:
    """Return the explorer-bucket label (server name) for an MCP tool.

    ``mcp__github__search_code`` → ``github``.
    ``github:search_code``       → ``github``.
    Non-MCP tools return the raw name unchanged.
    """
    if raw_name.startswith("mcp__"):
        parts = raw_name.split("__")
        return parts[1] if len(parts) >= 2 else raw_name
    if ":" in raw_name:
        left, _, _ = raw_name.partition(":")
        return left or raw_name
    return raw_name


# ── Tool result parsing ───────────────────────────────────────────────

def parse_tool_content(raw_content):
    """Normalise raw MCP tool result into a Python object.

    Handles: direct dict/list, JSON string, content-block list
    ``[{type: text, text: ...}]``, Python-repr single-quote strings,
    and "cat -n"-style line-numbered prefixes (Read tool output).
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
            return data

    if isinstance(data, dict):
        return data

    if not isinstance(data, str):
        return None

    text = data.strip()

    # Strip line-number prefixes from Read tool output (cat -n format)
    if re.match(r"^\d+[\t ]", text):
        lines = text.split("\n")
        stripped = []
        for line in lines:
            m = re.match(r"^\d+[\t ]+(.*)", line)
            stripped.append(m.group(1) if m else line)
        text = "\n".join(stripped).strip()

    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        # "Extra data" → valid JSON followed by trailing content;
        # truncate at the reported position and retry.
        if "Extra data" in str(e) and hasattr(e, "pos") and e.pos:
            try:
                parsed = json.loads(text[:e.pos])
            except (json.JSONDecodeError, ValueError):
                pass
        if parsed is None:
            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError):
                return None

    if parsed is None:
        return None

    # If result is a content-block list, extract text and re-parse.
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
                try:
                    return ast.literal_eval(inner)
                except (ValueError, SyntaxError):
                    pass
        return None

    return parsed


def extract_thumbnails_from_tool_result(raw_content) -> list:
    """Extract thumbnail URLs from MCP tool result content."""
    data = parse_tool_content(raw_content)
    if not data:
        return []

    thumbnails: list = []

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


def extract_tool_results_for_explorer(raw_content) -> list:
    """Extract structured results from MCP tool result for the explorer sidebar."""
    data = parse_tool_content(raw_content)
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
        if meta.get("error") or (
            item.get("content", "").startswith("오류 발생")
            or item.get("content", "").lower().startswith("error")
        ):
            continue
        url = (
            meta.get("url") or meta.get("edm_link")
            or item.get("url") or item.get("edm_link") or ""
        )
        if not url:
            links = meta.get("_links") or item.get("_links") or {}
            if links.get("webui"):
                space = meta.get("space") or {}
                space_links = space.get("_links") or {}
                base = ""
                if space_links.get("self"):
                    base = space_links["self"].split("/rest/")[0]
                url = f"{base}{links['webui']}" if base else links["webui"]
            elif meta.get("page_id") or meta.get("id"):
                page_id = meta.get("page_id") or meta.get("id")
                space = meta.get("space") or {}
                space_links = space.get("_links") or {}
                if space_links.get("self"):
                    base = space_links["self"].split("/rest/")[0]
                    url = f"{base}/pages/viewpage.action?pageId={page_id}"
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


def extract_tool_result_text(raw_content) -> str:
    """Extract plain text from a tool result content payload."""
    if not raw_content:
        return ""
    if isinstance(raw_content, list):
        parts = []
        for b in raw_content:
            if isinstance(b, dict):
                parts.append(b.get("text", ""))
            else:
                parts.append(str(b))
        return " ".join(parts).strip()

    text = str(raw_content).strip()
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


# ── Tag builders ─────────────────────────────────────────────────────

def build_gallery_tag(folder: str = "", current: str = "",
                      base_url: str = "", images: list | None = None) -> str:
    """Build a ``<details type='image_gallery'>`` tag for the frontend."""
    parts = ['type="image_gallery"', 'done="true"']
    if folder:
        parts.append(f'folder="{safe_attr(folder)}"')
    if current:
        parts.append(f'current="{safe_attr(current)}"')
    if base_url:
        parts.append(f'base_url="{safe_attr(base_url)}"')
    if images:
        safe_images = safe_attr(json.dumps(images, ensure_ascii=False))
        parts.append(f'images="{safe_images}"')
    attrs = " ".join(parts)
    return (
        f'\n\n<details {attrs}>\n'
        f'<summary>Image Gallery</summary>\n'
        f'</details>\n\n'
    )


def build_tool_explorer_tag(tool_data: dict) -> str:
    """Build a ``<details type='tool_explorer'>`` tag with JSON body."""
    body = json.dumps(tool_data, ensure_ascii=False)
    return (
        f'\n\n<details type="tool_explorer" done="true">\n'
        f'<summary>Tool Results</summary>\n'
        f'{body}\n'
        f'</details>\n\n'
    )


# ── Friendly tool notification helpers ────────────────────────────────
# Maps raw MCP tool-name suffix → friendly display name.
MCP_LABELS: dict = {
    "mlm_cql": "MLM Confluence",
    "cql": "Confluence",
    "basic_knowledge": "knowledge base",
    "jira_search": "Jira",
    "jira_issue": "Jira issue",
    "web_search": "the web",
    "slack_search": "Slack",
    "google_drive": "Google Drive",
}

BUILTIN_LABELS: dict = {
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

DONE_TEMPLATES: list = [
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

ERROR_TEMPLATES: list = [
    "Failed to search {label}",
    "Something went wrong with {label}",
    "Could not complete {label} search",
]


def tool_label(raw_name: str) -> str:
    """Return a short, human-friendly label for a tool name."""
    lower = raw_name.lower()
    if lower in BUILTIN_LABELS:
        return BUILTIN_LABELS[lower]
    if is_mcp_tool(raw_name):
        if raw_name.startswith("mcp__"):
            tool_key = raw_name.split("__")[-1]
        else:
            tool_key = raw_name.rpartition(":")[2]
        if tool_key.lower() in MCP_LABELS:
            return MCP_LABELS[tool_key.lower()]
        return tool_key.replace("_", " ")
    return raw_name


def friendly_tool_notification(raw_name: str, is_error: bool = False) -> str:
    """Build a single-tool notification (fallback when buffer is unavailable)."""
    label = tool_label(raw_name)
    if is_error:
        template = random.choice(ERROR_TEMPLATES)
        return f"❌ {template.format(label=label)}"
    template = random.choice(DONE_TEMPLATES)
    return f"✅ {template.format(label=label)}"
