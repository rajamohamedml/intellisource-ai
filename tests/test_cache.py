"""Tests for cache.py -- on-disk cache hit/miss behavior."""

from __future__ import annotations

import json
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
    key = compute_cache_key("rendered class text")

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
    assert compute_cache_key("class A") != compute_cache_key("class B")


def test_refresh_cache_ignores_existing_file(tmp_path: Path) -> None:
    cache_path = tmp_path / "llm_cache.json"
    cache = LLMCache(cache_path)
    key = compute_cache_key("rendered class text")
    cache.set(key, _sample_description())
    cache.save()

    refreshed = LLMCache(cache_path, load_existing=False)

    assert refreshed.get(key) is None


def test_corrupt_cache_file_degrades_to_empty_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "llm_cache.json"
    cache_path.write_text("not valid json{{{", encoding="utf-8")

    cache = LLMCache(cache_path)

    assert cache.get(compute_cache_key("anything")) is None


def test_malformed_entry_degrades_to_miss_instead_of_crashing(tmp_path: Path) -> None:
    cache_path = tmp_path / "llm_cache.json"
    key = compute_cache_key("rendered class text")
    cache_path.write_text(
        json.dumps({key: {"not_a_valid_field": "missing class_name/description"}}),
        encoding="utf-8",
    )

    cache = LLMCache(cache_path)
    result = cache.get(key)

    assert result is None
    assert cache.misses == 1
    assert cache.hits == 0


def test_malformed_entry_is_evicted_so_it_does_not_recur_on_save(tmp_path: Path) -> None:
    cache_path = tmp_path / "llm_cache.json"
    key = compute_cache_key("rendered class text")
    cache_path.write_text(
        json.dumps({key: {"not_a_valid_field": "missing class_name/description"}}),
        encoding="utf-8",
    )

    cache = LLMCache(cache_path)
    cache.get(key)
    cache.save()

    persisted = json.loads(cache_path.read_text(encoding="utf-8"))
    assert key not in persisted
