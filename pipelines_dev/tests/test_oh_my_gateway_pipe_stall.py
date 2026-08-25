"""_StallClock semantics: cut wedged (keepalive-only) streams, never live ones.

Review contract (PR #56): the TIMEOUT valve bounds SDK *silence*, not turn
length — (1) a keepalive-only stream times out, (2) a stream that keeps
producing real events runs past TIMEOUT untouched. This mirrors the
gateway's STREAM_STALL_TIMEOUT policy so the pipe's cap can never override
the gateway's "long agentic turns are unaffected" property.

The Pipe module imports httpx/pydantic, which aren't needed to test this
logic, so _StallClock (stdlib-only by design) is extracted from the source
via ast. Runs under pytest or directly: ``python3 test_oh_my_gateway_pipe_stall.py``.
"""

import ast
import pathlib
import time


def _load_helpers():
    src_path = pathlib.Path(__file__).resolve().parent.parent / "oh_my_gateway_pipe.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    wanted = {"_StallClock", "_read_timeout"}
    nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in wanted
    ]
    assert {node.name for node in nodes} == wanted
    namespace = {"time": time}
    module = ast.Module(body=nodes, type_ignores=[])
    exec(compile(module, str(src_path), "exec"), namespace)
    return namespace["_StallClock"], namespace["_read_timeout"]


_StallClock, _read_timeout = _load_helpers()


def test_keepalive_only_stream_times_out():
    """(1) A wedged turn — keepalive comments and blank separators only —
    must exhaust the budget: neither line kind resets the clock."""
    clock = _StallClock(0.05)
    deadline = time.monotonic() + 2.0  # test safety net
    tripped = False
    while time.monotonic() < deadline:
        if clock.note(": keepalive") or clock.note(""):
            tripped = True
            break
        time.sleep(0.01)
    assert tripped, "keepalive-only stream never hit the silence budget"


def test_real_events_keep_stream_alive_past_budget():
    """(2) A live turn streaming real events must run past TIMEOUT untouched.

    Total run (~8 * 0.02s) is several times the 0.05s budget; only a
    reset-per-real-event clock passes this.
    """
    clock = _StallClock(0.05)
    for i in range(8):
        time.sleep(0.02)
        assert clock.note("event: response.output_text.delta") is False
        assert clock.note('data: {"delta": "x"}') is False
        # The blank separator after a frame must not trip right after
        # progress either.
        assert clock.note("") is False


def test_keepalives_between_real_events_do_not_trip():
    """Keepalives interleaved with real events (the normal long-turn shape)
    never trip while real events keep landing inside the budget."""
    clock = _StallClock(0.08)
    for _ in range(4):
        time.sleep(0.03)
        assert clock.note(": keepalive") is False
        time.sleep(0.03)
        assert clock.note("data: {}") is False


def test_zero_budget_disables_the_guard():
    clock = _StallClock(0)
    clock.last_real -= 10_000  # ancient progress
    assert clock.note(": keepalive") is False


def test_zero_budget_also_disables_the_httpx_read_timeout():
    """TIMEOUT=0 must disable BOTH layers consistently: the stall clock above
    AND the httpx read timeout. read=0 would instead fail every read
    instantly — the opposite of 'disabled' (review: settings-semantics bug)."""
    assert _read_timeout(0) is None
    assert _read_timeout(-1) is None
    assert _read_timeout(600) == 600.0
    assert isinstance(_read_timeout(600), float)


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failed += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failed else 0)
