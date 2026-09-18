"""On-disk cache for per-class LLM extraction results.

Keyed by a hash of the exact prompt content that would be sent to the LLM
for one class (its rendered signature/annotation/complexity text, from
`chunker.render_class_for_prompt`) plus `PROMPT_VERSION` and the model ID.
This means:

  - An unchanged class on a re-run is a guaranteed cache hit — zero
    additional LLM cost for a repeated demo run or a re-run after editing
    unrelated files.
  - Bumping `PROMPT_VERSION` after changing the extraction prompt
    invalidates every cached entry at once, rather than silently mixing
    results produced under different prompt versions.
  - Switching `--model` invalidates every cached entry at once, rather
    than silently reusing a description produced by a different model.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from intellisource_ai.schemas import ClassDescription

logger = logging.getLogger(__name__)

# Bump this whenever the extraction prompt/instructions in llm_client.py
# change meaningfully enough that previously-cached descriptions should be
# regenerated rather than reused as-is.
PROMPT_VERSION = "v1"


def compute_cache_key(rendered_class_text: str, model: str) -> str:
    """Derive a stable cache key from one class's rendered prompt text and
    the model that will produce the description. Including `model` ensures
    switching `--model` naturally invalidates prior entries instead of
    silently reusing a description produced by a different model.
    """
    payload = f"{PROMPT_VERSION}:{model}:{rendered_class_text}".encode()
    return hashlib.sha256(payload).hexdigest()


class LLMCache:
    """A flat, on-disk JSON cache mapping `cache_key -> serialized ClassDescription`."""

    def __init__(self, path: Path, *, load_existing: bool = True) -> None:
        """
        Args:
            path: Where the cache file lives (created on first `save()`).
            load_existing: If False, start with an empty cache regardless
                of what's on disk — used for `--refresh-cache`.
        """
        self._path = path
        self._entries: dict[str, dict[str, Any]] = {}
        self._hits = 0
        self._misses = 0
        if load_existing:
            self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._entries = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt or unreadable cache file should degrade to "cold
            # start", never crash the run — the cache is an optimization,
            # not a source of truth.
            logger.warning("Ignoring unreadable cache file %s: %s", self._path, exc)
            self._entries = {}

    def get(self, key: str) -> ClassDescription | None:
        """Look up a cached result. Returns None (and records a miss) if
        absent or malformed — callers fall back to an LLM call in that case.
        A malformed entry (e.g. schema mismatch after a prompt change
        without a `PROMPT_VERSION` bump, or a hand-edited value) is evicted
        so it doesn't recur on `save()`.
        """
        if key not in self._entries:
            self._misses += 1
            return None
        try:
            result = ClassDescription.model_validate(self._entries[key])
        except ValidationError as exc:
            logger.warning("Ignoring malformed cache entry for key %s: %s", key, exc)
            del self._entries[key]
            self._misses += 1
            return None
        self._hits += 1
        return result

    def set(self, key: str, value: ClassDescription) -> None:
        """Record a freshly-computed LLM result for later reuse."""
        self._entries[key] = value.model_dump(mode="json")

    def save(self) -> None:
        """Persist the current cache contents to disk."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")

    @property
    def hits(self) -> int:
        """Number of `get()` calls this run that found a cached entry."""
        return self._hits

    @property
    def misses(self) -> int:
        """Number of `get()` calls this run that required an LLM call."""
        return self._misses
