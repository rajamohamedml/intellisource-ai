"""Tests for pipeline's token-estimation privacy boundary.

Raw repository source must never be transmitted to the Anthropic API --
not even to the `count_tokens` endpoint -- so `estimated_total_tokens` is a
local chars-per-token approximation. These tests record everything the
pipeline would have sent to `count_tokens` and fail if raw source appears.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from intellisource_ai import pipeline
from intellisource_ai.config import Settings
from intellisource_ai.exceptions import LLMExtractionError
from intellisource_ai.pipeline import (
    _ESTIMATED_CHARS_PER_TOKEN,
    _compute_token_savings_pct,
    _estimate_repo_tokens,
    run_pipeline,
)
from tests.conftest import FakeAnthropicClient

# Lives only inside a method body: the condensed prompt text
# (signatures/annotations/complexity) never contains it, so it appears in a
# count_tokens payload only if raw source is being sent.
_RAW_SOURCE_MARKER = "PROPRIETARY_METHOD_BODY_MARKER"

_JAVA_SOURCE = f'''package com.example.app.services.billing.service;

public class InvoiceService {{
    public String render() {{
        return "{_RAW_SOURCE_MARKER}";
    }}
}}
'''


class _RecordingAnthropicClient(FakeAnthropicClient):
    def __init__(self, **_: Any) -> None:
        super().__init__()
        self.sent_texts: list[str] = []
        original = self.messages.count_tokens

        def recording_count_tokens(*, model: str, messages: list[dict[str, str]]) -> Any:
            self.sent_texts.append(messages[0]["content"])
            return original(model=model, messages=messages)

        self.messages.count_tokens = recording_count_tokens  # type: ignore[method-assign]


class _FailingLLMClient:
    def __init__(self, **_: Any) -> None:
        pass

    def analyze_batch(self, prompt_text: str) -> Any:
        raise LLMExtractionError("offline test")

    def generate_overview(self, prompt_text: str) -> Any:
        raise LLMExtractionError("offline test")


def _settings(repo: Path, tmp_path: Path) -> Settings:
    return Settings(
        repo_url=None,
        repo_ref="HEAD",
        anthropic_api_key="test-key",
        model="claude-test",
        batch_size=6,
        token_ceiling_per_batch=4000,
        output_path=tmp_path / "output" / "analysis.json",
        include_tests=False,
        max_files=None,
        refresh_cache=True,
        refresh_repo=False,
        generate_html_report=False,
        cache_dir=tmp_path / "cache",
        local_path=repo,
    )


def test_run_pipeline_never_sends_raw_source_to_count_tokens(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    java_dir = repo / "src" / "main" / "java" / "com" / "example" / "app" / "services" / "billing" / "service"
    java_dir.mkdir(parents=True)
    (java_dir / "InvoiceService.java").write_text(_JAVA_SOURCE, encoding="utf-8")

    recorded: list[_RecordingAnthropicClient] = []

    def make_client(**kwargs: Any) -> _RecordingAnthropicClient:
        client = _RecordingAnthropicClient(**kwargs)
        recorded.append(client)
        return client

    monkeypatch.setattr(pipeline, "Anthropic", make_client)
    monkeypatch.setattr(pipeline, "LLMClient", _FailingLLMClient)

    analysis = run_pipeline(_settings(repo, tmp_path))

    sent_texts = recorded[0].sent_texts
    assert sent_texts, "condensed text should still be counted via the live tokenizer"
    assert not any(_RAW_SOURCE_MARKER in text for text in sent_texts)
    assert analysis.metadata.estimated_total_tokens > 0
    assert analysis.metadata.estimated_llm_tokens > 0


def test_estimate_repo_tokens_is_a_local_chars_per_token_approximation(tmp_path: Path) -> None:
    java_file = tmp_path / "A.java"
    config_file = tmp_path / "pom.xml"
    java_file.write_text("x" * 700, encoding="utf-8")
    config_file.write_text("y" * 350, encoding="utf-8")

    assert _estimate_repo_tokens([java_file], [config_file]) == round(1050 / _ESTIMATED_CHARS_PER_TOKEN)
    assert _estimate_repo_tokens([], []) == 0


def test_token_savings_is_zero_when_either_side_is_unavailable() -> None:
    assert _compute_token_savings_pct(1000, 250) == 75.0
    assert _compute_token_savings_pct(0, 250) == 0.0
    # A degraded (failed) live count of 0 must not read as a 100% saving now
    # that the raw baseline is always available locally.
    assert _compute_token_savings_pct(1000, 0) == 0.0
