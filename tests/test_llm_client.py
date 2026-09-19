"""Tests for the structured-output recovery path in llm_client.py.

Regression coverage for a real failure seen in production: on some
batches the model emits a list/object field as an escaped JSON string
(e.g. `"classes": "[{...}]"`) instead of native nested JSON, which fails
Pydantic validation (`Input should be a valid list`) even though the
data itself is intact and recoverable.
"""

from __future__ import annotations

from typing import Any

import pytest

from intellisource_ai.exceptions import LLMExtractionError
from intellisource_ai.llm_client import (
    BATCH_TOOL_CHOICE,
    BATCH_TOOL_DEFINITION,
    LLMClient,
    UsageTracker,
    _recover_stringified_fields,
)
from intellisource_ai.schemas import ClassBatchAnalysis


class _FakeRawMessage:
    def __init__(self, tool_calls: list[dict[str, Any]]) -> None:
        self.tool_calls = tool_calls
        self.usage_metadata = {"input_tokens": 10, "output_tokens": 5}


class _FakeChain:
    """Stands in for a LangChain `with_structured_output(..., include_raw=True)`
    chain -- only `.invoke` is used by `LLMClient`.
    """

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result

    def invoke(self, messages: list[tuple[str, str]]) -> dict[str, Any]:
        return self._result


_VALID_CLASS_DICT = {
    "class_name": "CustomActorRepository",
    "description": "Custom query methods for Actor entities.",
    "methods": [],
    "notable_aspects": [],
}


def _stringified_classes_arg() -> dict[str, Any]:
    """The exact shape of the production bug: `classes` arrives as a JSON
    string instead of a native array.
    """
    return {"classes": f"[{_VALID_CLASS_DICT}]".replace("'", '"')}


def _make_client() -> LLMClient:
    # __init__ builds a real ChatAnthropic, which requires no network call
    # to construct -- only .invoke() would touch the network, and neither
    # of these tests calls the real chain.
    tracker = UsageTracker(model="claude-haiku-4-5")
    return LLMClient(api_key="test-key", model="claude-haiku-4-5", usage_tracker=tracker)


def test_recover_stringified_fields_decodes_double_encoded_list() -> None:
    raw = _FakeRawMessage(tool_calls=[{"name": "ClassBatchAnalysis", "args": _stringified_classes_arg()}])
    recovered = _recover_stringified_fields(raw, ClassBatchAnalysis)
    assert recovered is not None
    assert recovered.classes[0].class_name == "CustomActorRepository"


def test_recover_stringified_fields_returns_none_when_unrecoverable() -> None:
    raw = _FakeRawMessage(tool_calls=[{"name": "ClassBatchAnalysis", "args": {"classes": "not json at all"}}])
    assert _recover_stringified_fields(raw, ClassBatchAnalysis) is None


def test_recover_stringified_fields_returns_none_with_no_tool_calls() -> None:
    raw = _FakeRawMessage(tool_calls=[])
    assert _recover_stringified_fields(raw, ClassBatchAnalysis) is None


def test_analyze_batch_recovers_from_stringified_classes_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through `LLMClient.analyze_batch`: a response that would
    previously raise `LLMExtractionError` is silently repaired.
    """
    client = _make_client()
    raw = _FakeRawMessage(tool_calls=[{"name": "ClassBatchAnalysis", "args": _stringified_classes_arg()}])
    monkeypatch.setattr(
        client,
        "_batch_chain",
        _FakeChain({"raw": raw, "parsed": None, "parsing_error": "Input should be a valid list"}),
    )

    result = client.analyze_batch("irrelevant prompt text")

    assert result.classes[0].class_name == "CustomActorRepository"
    assert client._usage.calls_made == 1


def test_analyze_batch_still_raises_when_unrecoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client()
    raw = _FakeRawMessage(tool_calls=[])
    monkeypatch.setattr(
        client,
        "_batch_chain",
        _FakeChain({"raw": raw, "parsed": None, "parsing_error": "totally broken"}),
    )

    with pytest.raises(LLMExtractionError, match="totally broken"):
        client.analyze_batch("irrelevant prompt text")


def test_usage_tracker_cost_for_known_model_uses_its_published_rate() -> None:
    tracker = UsageTracker(model="claude-haiku-4-5")
    tracker.record(1_000_000, 1_000_000)

    assert tracker.estimated_cost_usd == pytest.approx(1.00 + 5.00)


def test_usage_tracker_cost_is_none_for_unrecognized_model() -> None:
    tracker = UsageTracker(model="claude-not-a-real-model")
    tracker.record(1_000_000, 1_000_000)

    assert tracker.estimated_cost_usd is None


def test_batch_tool_reservation_matches_what_the_real_chain_sends() -> None:
    """`chunker.build_batches` reserves `BATCH_TOOL_DEFINITION`'s tokens from
    the ceiling; that only holds while it equals what LangChain's
    `with_structured_output` really binds onto every batch request. Fails
    loudly if a langchain upgrade (or a change to how the chain is built)
    makes the two drift apart.
    """
    client = LLMClient(api_key="test-key", model="claude-haiku-4-5", usage_tracker=UsageTracker(model="m"))
    bound_kwargs = client._batch_chain.first.steps__["raw"].kwargs  # type: ignore[attr-defined]

    assert bound_kwargs["tools"] == [BATCH_TOOL_DEFINITION]
    assert bound_kwargs["tool_choice"] == BATCH_TOOL_CHOICE
