"""Inlined tool-content limit: keep the message body small, fail safe on junk.

Why this is load-bearing rather than cosmetic: the pipe embeds each tool
call's ``arguments`` and ``result`` into the assistant message as attributes
on a ``<details type="tool_calls">`` block. Open WebUI re-lexes that whole
message body once per animation frame while the turn streams (and again on
every reload of the chat), so per-frame cost scales with the body and the
per-turn cost with its square — an agentic turn with dozens of MCP calls is
what wedges the browser. TOOL_CONTENT_MAX_CHARS is the only knob that shrinks
``n`` itself, so its two contracts are pinned here:

  1. Truncation actually bounds the inlined text, and says how much it dropped.
  2. An unusable limit (blank valve, a typo in the client field, ``True``)
     falls back to the admin default — never to "unlimited", which would
     silently restore the hang the limit exists to prevent.

Stdlib-only by design, matching test_oh_my_gateway_pipe_stall.py: the Pipe
module imports httpx/pydantic, so the helpers under test are extracted from
the source via ast. Runs under pytest or directly with ``python3``.
"""

import ast
import pathlib
import typing

SRC_PATH = pathlib.Path(__file__).resolve().parent.parent / "oh_my_gateway_pipe.py"


def _load():
    """Extract the truncation helper plus the Pipeline's limit-resolution
    methods, rehosted on a stub class so no pydantic/httpx import is needed."""
    tree = ast.parse(SRC_PATH.read_text(encoding="utf-8"))

    top_level = {"_truncate_tool_content"}
    constants = {"_TOOL_CONTENT_HARD_LIMIT"}
    methods = {"_coerce_content_limit", "_resolve_tool_content_limit", "_tool_content_limit"}

    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in top_level
    ]
    nodes += [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) in constants for t in node.targets)
    ]
    assert {n.name for n in nodes if isinstance(n, ast.FunctionDef)} == top_level

    pipeline = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Pipeline"
    )
    method_nodes = [
        n for n in pipeline.body if isinstance(n, ast.FunctionDef) and n.name in methods
    ]
    assert {n.name for n in method_nodes} == methods, {n.name for n in method_nodes}

    stub = ast.ClassDef(
        name="_Stub",
        bases=[],
        keywords=[],
        body=method_nodes,
        decorator_list=[],
        type_params=[],
    )
    module = ast.fix_missing_locations(ast.Module(body=nodes + [stub], type_ignores=[]))
    namespace = {"Optional": typing.Optional}
    exec(compile(module, str(SRC_PATH), "exec"), namespace)
    return namespace


_NS = _load()
_truncate = _NS["_truncate_tool_content"]
_HARD_LIMIT = _NS["_TOOL_CONTENT_HARD_LIMIT"]
_Stub = _NS["_Stub"]


class _Valves:
    def __init__(self, limit):
        self.TOOL_CONTENT_MAX_CHARS = limit


class _Local:
    pass


def _pipe(valve_limit=200):
    p = _Stub()
    p.valves = _Valves(valve_limit)
    p._local = _Local()
    return p


# ── 1. truncation bounds the inlined text ───────────────────────────────────


def test_truncates_to_the_limit_and_reports_the_drop():
    out = _truncate("R" * 40000, 200)
    assert out.startswith("R" * 200)
    assert not out.startswith("R" * 201)
    # The reader must be able to tell a 200-char preview from a 200-char result.
    assert "40,000 chars total" in out
    assert len(out) < 300


def test_text_under_the_limit_is_returned_verbatim():
    assert _truncate("short result", 200) == "short result"
    assert _truncate("", 200) == ""


def test_disabled_limit_still_honours_the_hard_cap():
    """0 means "no valve limit" (the file's 0=disabled convention), but a
    single runaway result must never be shipped to the browser whole."""
    out = _truncate("x" * (_HARD_LIMIT * 5), 0)
    assert out.startswith("x" * _HARD_LIMIT)
    assert not out.startswith("x" * (_HARD_LIMIT + 1))


# ── 2. resolution order, and fail-safe on junk ──────────────────────────────


def test_client_setting_wins_over_the_valve():
    assert _pipe(200)._resolve_tool_content_limit({"tool_content_limit": 1000}, {}) == 1000
    # strings arrive from the wire
    assert _pipe(200)._resolve_tool_content_limit({}, {"tool_content_limit": "50"}) == 50


def test_valve_is_used_when_the_client_says_nothing():
    assert _pipe(350)._resolve_tool_content_limit({}, {}) == 350


def test_explicit_zero_from_the_client_disables_the_limit():
    """0 is a real choice ("Unlimited" in the UI), not a missing value."""
    assert _pipe(200)._resolve_tool_content_limit({"tool_content_limit": 0}, {}) == 0


def test_unusable_client_values_fall_back_to_the_valve_not_to_unlimited():
    for junk in ("", "abc", None, True, [], {}):
        limit = _pipe(200)._resolve_tool_content_limit({"tool_content_limit": junk}, {})
        assert limit == 200, (junk, limit)


def test_render_path_falls_back_to_the_valve_without_a_resolved_turn():
    """_render_system_event reads the thread-local; if a generator is resumed
    on a thread that never resolved this turn, the admin default must apply."""
    assert _pipe(200)._tool_content_limit() == 200


if __name__ == "__main__":
    import sys

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
