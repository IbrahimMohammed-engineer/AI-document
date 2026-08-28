"""
Unit tests — generator prompt assembly + streaming (Phase 9, Backend §34).

Covers: centralized message construction (system first, ORIGINAL question
— never the rewritten query — in the final user turn, bounded history),
generation config plumbing, streaming token accumulation with usage, and
the InsufficientEvidenceError typed result.
"""
from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.infrastructure.llm import LLMMessage, StubLLMProvider
from app.rag.context_builder import build_context
from app.rag.generator import (
    InsufficientEvidenceError,
    build_generation_messages,
    generate_answer,
    stream_answer,
)
from tests.unit.test_context_builder import _result


@pytest.fixture()
def bundle() -> object:
    results = [
        _result("c1", "alpha evidence", 0.9),
        _result("c2", "beta evidence", 0.7),
    ]
    return build_context(results)


@pytest.mark.unit
class TestBuildGenerationMessages:

    def test_system_prompt_is_first_message(self, bundle):
        messages = build_generation_messages(bundle, [], "What is the process?")
        assert messages[0].role == "system"
        assert "SOURCE blocks" in messages[0].content
        assert "never" in messages[0].content  # injection-hierarchy language present

    def test_original_question_in_final_user_message(self, bundle):
        messages = build_generation_messages(bundle, [], "What is the approval process?")
        last = messages[-1]
        assert last.role == "user"
        assert "What is the approval process?" in last.content
        assert "SOURCE 1" in last.content
        assert "SOURCE 2" in last.content

    def test_rewritten_query_never_substituted(self, bundle):
        """The drift-guard invariant: generation receives the user's words."""
        messages = build_generation_messages(
            bundle, [], "What about approval?"
        )
        assert "What about approval?" in messages[-1].content

    def test_history_bounded_to_configured_turns(self, bundle):
        settings = get_settings()
        max_messages = settings.llm_history_turns * 2
        history = [
            m
            for i in range(10)
            for m in (
                LLMMessage(role="user", content=f"q{i}"),
                LLMMessage(role="assistant", content=f"a{i}"),
            )
        ]
        messages = build_generation_messages(bundle, history, "q")
        non_system = [m for m in messages if m.role != "system"]
        assert len(non_system) <= max_messages + 1  # +1 the final question
        # the KEPT history is the most recent
        assert "a9" in messages[-2].content

    def test_empty_history_messages_filtered(self, bundle):
        history = [LLMMessage(role="assistant", content="   ")]
        messages = build_generation_messages(bundle, history, "q")
        assert all(m.content.strip() for m in messages)


@pytest.mark.unit
class TestGenerateAnswer:

    async def test_non_streaming_answer(self, bundle):
        provider = StubLLMProvider(responder=lambda msgs: "Grounded answer. [1]")
        answer = await generate_answer(bundle, [], "question", provider=provider)
        assert answer.text == "Grounded answer. [1]"
        assert answer.model == "stub-llm"
        assert answer.prompt_tokens > 0
        assert answer.completion_tokens > 0

    async def test_streaming_accumulates_words(self, bundle):
        provider = StubLLMProvider(responder=lambda msgs: "one two three")
        chunks = [c async for c in stream_answer(bundle, [], "q", provider=provider)]
        text = "".join(c.delta for c in chunks)
        assert text == "one two three"
        assert chunks[-1].completion_tokens == 3

    async def test_generation_receives_full_prompt(self, bundle):
        recorded = {}

        def responder(msgs):
            recorded["contents"] = [m.content for m in msgs]
            return "ok [1]"

        provider = StubLLMProvider(responder=responder)
        await generate_answer(bundle, [], "the question", provider=provider)
        joined = "\n".join(recorded["contents"])
        assert "alpha evidence" in joined  # context reached the LLM
        assert "the question" in joined
        assert "c1" not in joined  # chunk ids never leak into the prompt


@pytest.mark.unit
class TestInsufficientEvidenceError:

    def test_is_typed_success_shape_not_http_error(self):
        exc = InsufficientEvidenceError()
        assert exc.groundedness == "ungrounded"
        assert "couldn't find enough information" in exc.message
        # Intentionally NOT an AppException subclass — no http_status
        assert not hasattr(exc, "http_status")
