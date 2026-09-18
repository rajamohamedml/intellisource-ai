"""Tests for chunker.py -- batch-size and token-ceiling limits, and the
oversized-class-splitting fallback. Uses `fake_anthropic_client` (see
conftest.py) instead of a real Anthropic client, so no network call is made.

Token ceilings in the ceiling-specific tests are derived from the fake
tokenizer's own measurement of the fixtures, rather than hardcoded numbers
-- this keeps the tests correct even if the prompt-rendering format in
`chunker.render_class_for_prompt` changes length later.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from intellisource_ai.chunker import build_batches, render_class_for_prompt
from intellisource_ai.exceptions import LLMExtractionError
from intellisource_ai.schemas import ClassType, ParsedClass, ParsedMethod

from .conftest import FakeAnthropicClient

_MODEL = "claude-haiku-4-5"

_SEPARATOR_OVERHEAD_TOKENS = 10


def _non_additive_tokens(text: str) -> int:
    # Each "\n\n" (the join between classes in a multi-class batch) costs
    # extra tokens that no per-class count ever sees -- exactly the drift
    # the additive per-class running total in `_batch_group` can't detect.
    return max(1, len(text) // 4) + _SEPARATOR_OVERHEAD_TOKENS * text.count("\n\n")


class _NonAdditiveClient:
    """Counts tokens with `_non_additive_tokens` and records every call."""

    def __init__(self) -> None:
        self.counted_texts: list[str] = []
        self.messages = SimpleNamespace(count_tokens=self._count_tokens)

    def _count_tokens(self, *, model: str, messages: list[dict[str, str]]) -> SimpleNamespace:
        text = messages[0]["content"]
        self.counted_texts.append(text)
        return SimpleNamespace(input_tokens=_non_additive_tokens(text))


def _make_class(name: str, method_count: int) -> ParsedClass:
    methods = [
        ParsedMethod(
            name=f"method{i}",
            signature=f"void method{i}()",
            return_type="void",
            parameters=[],
            modifiers=["public"],
            annotations=[],
            javadoc=None,
            start_line=i + 1,
            end_line=i + 2,
        )
        for i in range(method_count)
    ]
    return ParsedClass(
        file_path=f"src/main/java/pkg/{name}.java",
        package="pkg",
        class_name=name,
        class_type=ClassType.SERVICE,
        javadoc=None,
        annotations=[],
        methods=methods,
        rest_endpoints=[],
    )


def _tokens_for(client: FakeAnthropicClient, cls: ParsedClass) -> int:
    return client.messages.count_tokens(
        model=_MODEL, messages=[{"role": "user", "content": render_class_for_prompt(cls, {})}]
    ).input_tokens


def test_batch_size_cap_is_respected(fake_anthropic_client: FakeAnthropicClient) -> None:
    classes = [_make_class(f"Class{i}", method_count=1) for i in range(10)]

    batches = build_batches(
        classes,
        complexity_index={},
        anthropic_client=fake_anthropic_client,
        model=_MODEL,
        batch_size=3,
        token_ceiling=1_000_000,  # effectively unlimited: batch_size alone is the limiting factor
    )

    assert sum(len(b.classes) for b in batches) == 10
    assert all(len(b.classes) <= 3 for b in batches)


def test_token_ceiling_forces_separate_batches(fake_anthropic_client: FakeAnthropicClient) -> None:
    classes = [_make_class(f"Class{i}", method_count=2) for i in range(4)]
    single_class_tokens = _tokens_for(fake_anthropic_client, classes[0])

    # Comfortably fits exactly one class but never two -- forces every
    # class into its own batch without tripping the oversized-single-class
    # split path (each class alone is well under this ceiling).
    token_ceiling = int(single_class_tokens * 1.5)

    batches = build_batches(
        classes,
        complexity_index={},
        anthropic_client=fake_anthropic_client,
        model=_MODEL,
        batch_size=100,
        token_ceiling=token_ceiling,
    )

    assert sum(len(b.classes) for b in batches) == 4
    assert len(batches) == 4
    assert all(len(b.classes) == 1 for b in batches)


def test_oversized_single_class_is_split_not_truncated(fake_anthropic_client: FakeAnthropicClient) -> None:
    huge_class = _make_class("HugeClass", method_count=40)
    small_variant_tokens = _tokens_for(fake_anthropic_client, _make_class("HugeClass", method_count=2))
    huge_tokens = _tokens_for(fake_anthropic_client, huge_class)

    # Comfortably between a small slice of the class and the whole thing --
    # guarantees the whole-class render trips the oversized path.
    token_ceiling = (huge_tokens + small_variant_tokens) // 2

    batches = build_batches(
        [huge_class],
        complexity_index={},
        anthropic_client=fake_anthropic_client,
        model=_MODEL,
        batch_size=100,
        token_ceiling=token_ceiling,
    )

    assert len(batches) > 1
    all_method_names = {
        method.name for batch in batches for cls in batch.classes for method in cls.methods
    }
    assert all_method_names == {f"method{i}" for i in range(40)}


def test_assembled_batch_over_ceiling_is_resplit_not_dispatched() -> None:
    classes = [_make_class(f"Class{i}", method_count=2) for i in range(3)]
    client = _NonAdditiveClient()
    single_class_tokens = _non_additive_tokens(render_class_for_prompt(classes[0], {}))

    # The three per-class counts sum to exactly the ceiling, so the additive
    # running total lets all three into one batch -- but the assembled text
    # also pays for the "\n\n" separators and lands over the ceiling.
    token_ceiling = single_class_tokens * 3
    assert _non_additive_tokens("\n\n".join(render_class_for_prompt(c, {}) for c in classes)) > token_ceiling

    batches = build_batches(
        classes,
        complexity_index={},
        anthropic_client=client,  # type: ignore[arg-type]
        model=_MODEL,
        batch_size=100,
        token_ceiling=token_ceiling,
    )

    assert len(batches) > 1
    assert all(_non_additive_tokens(b.prompt_text) <= token_ceiling for b in batches)
    assert [c.class_name for b in batches for c in b.classes] == [c.class_name for c in classes]


def test_verification_adds_one_count_per_multi_class_batch_only() -> None:
    classes = [_make_class(f"Class{i}", method_count=1) for i in range(6)]
    client = _NonAdditiveClient()

    batches = build_batches(
        classes,
        complexity_index={},
        anthropic_client=client,  # type: ignore[arg-type]
        model=_MODEL,
        batch_size=3,
        token_ceiling=1_000_000,
    )

    assert len(batches) == 2
    # 6 per-class counts + 1 verification count for each of the 2 multi-class
    # batches; no per-addition re-measurement of the growing batch text.
    assert len(client.counted_texts) == 6 + 2
    assert all(b.prompt_text in client.counted_texts for b in batches)


def test_single_class_batches_are_not_reverified() -> None:
    classes = [_make_class(f"Class{i}", method_count=1) for i in range(3)]
    client = _NonAdditiveClient()

    batches = build_batches(
        classes,
        complexity_index={},
        anthropic_client=client,  # type: ignore[arg-type]
        model=_MODEL,
        batch_size=1,
        token_ceiling=1_000_000,
    )

    assert len(batches) == 3
    assert len(client.counted_texts) == 3


def test_system_prompt_tokens_are_reserved_from_ceiling(fake_anthropic_client: FakeAnthropicClient) -> None:
    classes = [_make_class(f"Class{i}", method_count=4) for i in range(2)]
    class_tokens = _tokens_for(fake_anthropic_client, classes[0])
    system_prompt = "x" * (4 * class_tokens // 2)
    system_tokens = fake_anthropic_client.messages.count_tokens(
        model=_MODEL, messages=[{"role": "user", "content": system_prompt}]
    ).input_tokens

    # Both classes fit under the raw ceiling with room to spare, but not once
    # the system prompt's tokens are reserved; each class alone still fits.
    token_ceiling = 2 * class_tokens + 10
    assert 2 * class_tokens + 10 - system_tokens < 2 * class_tokens
    assert 2 * class_tokens + 10 - system_tokens >= class_tokens

    def build(prompt: str) -> list[int]:
        batches = build_batches(
            classes,
            complexity_index={},
            anthropic_client=fake_anthropic_client,
            model=_MODEL,
            batch_size=100,
            token_ceiling=token_ceiling,
            system_prompt=prompt,
        )
        return [len(b.classes) for b in batches]

    assert build("") == [2]
    assert build(system_prompt) == [1, 1]


def test_system_prompt_reservation_applies_to_assembled_batch_verification() -> None:
    classes = [_make_class(f"Class{i}", method_count=2) for i in range(2)]
    client = _NonAdditiveClient()
    class_tokens = _non_additive_tokens(render_class_for_prompt(classes[0], {}))
    system_prompt = "x" * 40
    system_tokens = _non_additive_tokens(system_prompt)

    # The assembled text fits the raw ceiling by exactly the system prompt's
    # cost minus one token, so it overshoots only the *reduced* ceiling. The
    # additive per-class sum still fits that reduced ceiling, so only the
    # reserved-ceiling verification can catch it.
    assembled = _non_additive_tokens("\n\n".join(render_class_for_prompt(c, {}) for c in classes))
    token_ceiling = assembled + system_tokens - 1
    assert 2 * class_tokens <= token_ceiling - system_tokens < assembled

    batches = build_batches(
        classes,
        complexity_index={},
        anthropic_client=client,  # type: ignore[arg-type]
        model=_MODEL,
        batch_size=100,
        token_ceiling=token_ceiling,
        system_prompt=system_prompt,
    )

    assert [len(b.classes) for b in batches] == [1, 1]
    assert all(_non_additive_tokens(b.prompt_text) + system_tokens <= token_ceiling for b in batches)


def test_system_prompt_is_counted_once_not_per_batch() -> None:
    classes = [_make_class(f"Class{i}", method_count=1) for i in range(6)]
    client = _NonAdditiveClient()
    system_prompt = "You are analyzing Java classes."

    build_batches(
        classes,
        complexity_index={},
        anthropic_client=client,  # type: ignore[arg-type]
        model=_MODEL,
        batch_size=3,
        token_ceiling=1_000_000,
        system_prompt=system_prompt,
    )

    assert client.counted_texts.count(system_prompt) == 1
    # 1 system prompt + 6 per-class + 2 batch verifications.
    assert len(client.counted_texts) == 1 + 6 + 2


def test_system_prompt_larger_than_ceiling_fails_fast(fake_anthropic_client: FakeAnthropicClient) -> None:
    with pytest.raises(LLMExtractionError, match="leaving no room"):
        build_batches(
            [_make_class("Class0", method_count=1)],
            complexity_index={},
            anthropic_client=fake_anthropic_client,
            model=_MODEL,
            batch_size=3,
            token_ceiling=10,
            system_prompt="x" * 400,
        )
