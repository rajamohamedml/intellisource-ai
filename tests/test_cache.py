"""Tests for cache.py -- on-disk cache hit/miss behavior."""

from __future__ import annotations

from pathlib import Path

from intellisource_ai.cache import LLMCache, compute_cache_key
from intellisource_ai.schemas import ClassDescription, MethodDescription


def _sample_description() -> ClassDescription:
    return ClassDescription(
        class_name="Widget",
        description="A widget.",
        methods=[MethodDescription(signature="void doIt()", description="Does it.")],
        notable_aspects=[],
    )


def test_miss_then_hit_round_trip(tmp_path: Path) -> None:
    cache_path = tmp_path / "llm_cache.json"
    cache = LLMCache(cache_path)
    key = compute_cache_key("rendered class text", "claude-sonnet-4-6")

    assert cache.get(key) is None
    assert cache.misses == 1

    cache.set(key, _sample_description())
    cache.save()

    reloaded = LLMCache(cache_path)
    result = reloaded.get(key)

    assert result is not None
    assert result.class_name == "Widget"
    assert reloaded.hits == 1


def test_different_content_produces_different_keys() -> None:
    model = "claude-sonnet-4-6"
    assert compute_cache_key("class A", model) != compute_cache_key("class B", model)


def test_different_model_produces_different_keys_for_same_content(tmp_path: Path) -> None:
    """Regression test for issue #7: switching --model must invalidate the
    cache for otherwise-unchanged class text, not silently reuse a
    description produced by a different model.
    """
    key_a = compute_cache_key("rendered class text", "claude-haiku-4-5")
    key_b = compute_cache_key("rendered class text", "claude-sonnet-4-6")

    assert key_a != key_b

    cache = LLMCache(tmp_path / "llm_cache.json", load_existing=False)
    cache.set(key_a, _sample_description())

    assert cache.get(key_a) is not None
    assert cache.get(key_b) is None


def test_refresh_cache_ignores_existing_file(tmp_path: Path) -> None:
    cache_path = tmp_path / "llm_cache.json"
    cache = LLMCache(cache_path)
    key = compute_cache_key("rendered class text", "claude-sonnet-4-6")
    cache.set(key, _sample_description())
    cache.save()

    refreshed = LLMCache(cache_path, load_existing=False)

    assert refreshed.get(key) is None


def test_corrupt_cache_file_degrades_to_empty_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "llm_cache.json"
    cache_path.write_text("not valid json{{{", encoding="utf-8")

    cache = LLMCache(cache_path)

    assert cache.get(compute_cache_key("anything", "claude-sonnet-4-6")) is None
