"""How a turn ENDS: the question must survive the injected protocol, and a
turn that lost its context or got cut short must say so.

The reported failure: on a multi-part question the agent searched fine, wrote
its MEMORY.md entry fine, and then closed the turn with meta-narration ("what
was I doing… there is no user question, how can I help?") which the pipe
rendered as the answer. Nothing was broken downstream — the prompt was. The
pipe appends ~3.5 KB of imperative protocol AFTER the user's question, and the
MEMORY protocol's last numbered step was a marker plus the response token, so
by recency the model's terminal instruction was "protocol complete", not
"answer the question". Once the response token is emitted the pipe treats
everything after it as the reply, so the narration became the answer.

Three contracts are pinned here:

  1. The question is restated AFTER every injected block, so the terminal
     instruction is the task and not the bookkeeping.
  2. The protocol text carries no bare response token on its own line — the
     pipe splits on the FIRST one it sees in the output, so an example the
     model can imitate is a live footgun, not a documentation detail.
  3. Compaction and a max_turns cutoff are rendered. Both were dropped, which
     is what made "the turn forgot the question" indistinguishable from "the
     model answered badly" — the one distinction a reader needs.

Stdlib-only by design, matching the sibling suites: the Pipe module imports
httpx/pydantic, so the helpers under test are extracted from the source via
ast. Runs under pytest or directly with ``python3``.
"""

import ast
import pathlib
import typing

SRC_PATH = pathlib.Path(__file__).resolve().parent.parent / "oh_my_gateway_pipe.py"

RESPONSE_TAG = "<response>"


def _load():
    tree = ast.parse(SRC_PATH.read_text(encoding="utf-8"))

    constants = {"_INCOMPLETE_NOTICE"}
    methods = {
        "_append_turn_instructions",
        "_get_final_question_block",
        "_get_thought_wrapped_instruction",
        "_get_memory_reference_instruction",
        "_get_memory_update_instruction",
        "_render_compaction",
    }

    nodes = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) in constants for t in node.targets)
    ]
    assert len(nodes) == len(constants), "missing _INCOMPLETE_NOTICE"

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
_Stub = _NS["_Stub"]
_INCOMPLETE_NOTICE = _NS["_INCOMPLETE_NOTICE"]


class _Valves:
    def __init__(self, **kw):
        self.OUTPUT_FORMAT = kw.get("OUTPUT_FORMAT", "thought_wrapped")
        self.THOUGHT_WRAPPED_INSTRUCTION = kw.get("THOUGHT_WRAPPED_INSTRUCTION", True)
        self.MEMORY_REFERENCE_PROMPT = kw.get("MEMORY_REFERENCE_PROMPT", True)
        self.MEMORY_UPDATE_PROMPT = kw.get("MEMORY_UPDATE_PROMPT", True)


def _pipe(**kw):
    p = _Stub()
    p.valves = _Valves(**kw)
    return p


QUESTION = "ㅇㅇ이 뭐야 그리고 ㅁㅁ 와의 연관성도 알려줘"


# ── 1. the question outlives the protocol ───────────────────────────────────


def test_question_is_restated_after_every_injected_block():
    out = _pipe()._append_turn_instructions(QUESTION, None, QUESTION)
    # Both protocol blocks are present…
    assert "MEMORY.md 업데이트 프로토콜" in out
    assert "MEMORY.md 활용" in out
    # …and the question comes back AFTER them. Recency is the whole point: if
    # the protocol is last, the model's terminal instruction is bookkeeping.
    assert out.rindex(QUESTION) > out.index("MEMORY.md 업데이트 프로토콜")
    assert out.rindex("<user_question>") > out.index("MEMORY.md 업데이트 프로토콜")


def test_restatement_is_skipped_when_nothing_was_injected():
    """A task turn (title/tags/follow-up) gets no protocol, so it needs no
    restatement — Open WebUI already embeds the chat into the task prompt, and
    appending a second copy would just inflate it."""
    out = _pipe()._append_turn_instructions(QUESTION, "title_generation", QUESTION)
    assert out == QUESTION
    assert "지금 답할 질문" not in out


def test_restatement_survives_a_partially_disabled_valve_set():
    """Read-only memory mode still injects a block, so the question still has
    to be restated after it."""
    out = _pipe(MEMORY_UPDATE_PROMPT=False)._append_turn_instructions(
        QUESTION, None, QUESTION
    )
    assert "MEMORY.md 업데이트 프로토콜" not in out
    assert out.rindex(QUESTION) > out.index("MEMORY.md 활용")


def test_blank_question_adds_no_empty_restatement():
    """An empty <user_question> block would point the model at nothing, which
    is worse than not pointing it anywhere. (The phrase itself also appears in
    the protocol prose, so the tag is what identifies the block.)"""
    out = _pipe()._append_turn_instructions("   ", None, "   ")
    assert "<user_question>" not in out


# ── 2. no imitable response token in the prompt ─────────────────────────────


def test_protocol_text_carries_no_bare_response_token_line():
    """The pipe closes <thought> on the FIRST response token in the OUTPUT, so
    a token sitting on its own line in the prompt is something the model can
    copy early — after which its remaining internal narration renders as the
    answer. The blocks may NAME the token in prose; none may present it as a
    line to reproduce."""
    p = _pipe()
    for name in (
        "_get_thought_wrapped_instruction",
        "_get_memory_reference_instruction",
        "_get_memory_update_instruction",
    ):
        block = getattr(p, name)()
        bare = [ln for ln in block.splitlines() if ln.strip() == RESPONSE_TAG]
        assert not bare, f"{name} has a bare {RESPONSE_TAG} line: {bare}"


def test_memory_protocol_does_not_end_on_the_response_token():
    """Its final numbered step used to be "output the token", full stop. The
    answer requirement lived 2.5 KB earlier and lost on recency."""
    block = _pipe()._get_memory_update_instruction()
    assert "최종 답변" in block.split("5.", 1)[1]


# ── 3. a lost or cut turn says so ───────────────────────────────────────────


def test_compaction_start_and_end_are_both_rendered():
    p = _pipe()
    assert "압축 중" in p._render_compaction({"type": "compaction", "phase": "start"})
    end = p._render_compaction(
        {"phase": "end", "trigger": "auto", "pre_tokens": 186_000, "post_tokens": 1_600}
    )
    assert "186,000" in end and "1,600" in end and "자동" in end


def test_compaction_invents_no_numbers_without_them():
    """An older gateway sends the past-tense boundary with no metadata. Saying
    that compaction happened is honest; making up a size is not."""
    out = _pipe()._render_compaction({"phase": "end", "trigger": None})
    assert "압축" in out
    assert not any(ch.isdigit() for ch in out)


def test_incomplete_notice_covers_max_turns_and_an_unseen_reason():
    assert "max_turns" in _INCOMPLETE_NOTICE
    assert "" in _INCOMPLETE_NOTICE, "no fallback for a reason this pipe has not seen"
    # The fallback must actually surface the reason it did not recognise.
    assert "sdk_wedged" in _INCOMPLETE_NOTICE[""].format(reason="sdk_wedged")
    # And it must not claim the work was lost — the partial output is real.
    assert "끊겼" in _INCOMPLETE_NOTICE["max_turns"]


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
