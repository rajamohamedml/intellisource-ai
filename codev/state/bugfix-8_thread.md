# bugfix-8 thread

## Issue #8: LLMCache.get() can crash the run on a malformed/schema-mismatched cache entry

**Root cause**: `LLMCache.get()` (`src/intellisource_ai/cache.py`) called
`ClassDescription.model_validate(raw)` unguarded. A schema-mismatched entry raised
`pydantic.ValidationError` uncaught through `pipeline.py:233`, aborting the whole run —
contradicting the module's documented "degrade to cold start, never crash" cache philosophy,
which was only enforced at the whole-file `_load()` level, not per-entry.

**Fix**: wrapped `model_validate()` in try/except `ValidationError`; on failure, log a warning,
delete the bad entry (so it doesn't recur on `save()`), record a miss, return `None`.

**Regression tests** (`tests/test_cache.py`): malformed-dict entry degrades to a miss and is
evicted on save; verified to fail pre-fix and pass post-fix.

### Environment gotcha (worth knowing for future builders)

Plain `pytest`/`ruff`/`mypy` in this worktree resolve to the **main checkout's** editable
install (`.venv` at repo root is shared across worktrees; the editable-install pointer is an
absolute path baked in at install time, not path-relative). Running bare `pytest` here silently
tests the main repo's `cache.py`, not this worktree's edits — a false negative if not caught.

Fix used: created a **local `.venv` inside this worktree** (`.venv/` is gitignored, so this
doesn't touch git state) and `pip install -e ".[dev]"` from within it, so it resolves to the
worktree's own `src/`. For porch's checks (which shell out to bare `pytest`/`ruff`/`mypy`), I
prefixed `PATH="$(pwd)/.venv/bin:$PATH"` onto the `porch done`/`porch next` invocation so the
child process picks up the worktree-local venv first. Confirmed this doesn't touch the shared
`.venv` — only additive, no other worktree was running concurrently.

### Mid-review conflict (surfaced by claude CMAP)

Branch was 9 commits behind master when the `pr` phase CMAP review ran. Master's bugfix-7
(PR #14) had changed `compute_cache_key(text)` -> `compute_cache_key(text, model)` in the
interim. Merged `origin/master`, resolved the one real conflict in `tests/test_cache.py` by
adding the `model` arg to my two new tests' `compute_cache_key()` calls; `cache.py` auto-merged
cleanly (my try/except and master's model-aware key touch disjoint code). Also narrowed the
`get()` docstring per claude's minor note (it overclaimed "corrupted cache file" scope that
belongs to `_load()`, not per-entry validation).

### Second real bug found by codex CMAP (post-merge)

`self._entries.get(key)` couldn't distinguish "key absent" from "key present with JSON `null`
value" — a `null` entry was silently treated as a permanent, never-evicted miss (not a crash,
but violates the "evicted so it doesn't recur" promise in the fix). Fixed by switching the
presence check to `key not in self._entries`, routing a stored `null` through the same
validate/evict path (`ClassDescription.model_validate(None)` also raises `ValidationError`,
confirmed). Added `test_null_entry_degrades_to_miss_and_is_evicted`, verified fail-then-pass.

### CMAP verdicts (final, on commit 2c474c8)

- gemini: skipped — `agy` CLI not installed in this environment (non-blocking, same both runs)
- codex: APPROVE
- claude: APPROVE — flagged one non-blocking follow-up-worthy gap: `_load()` still doesn't
  guard against valid-JSON-but-non-dict top-level cache content (`5`, `[1,2]`, `"hello"` all
  still raise `TypeError` in `get()`). Genuinely a different root cause than #8 (top-of-file
  load guard vs. per-entry validation) — did not expand this PR's scope for it. Worth a
  follow-up issue if the architect wants it filed.

**Status**: PR #16 pushed, CMAP complete, `pr` gate reached. Waiting for human approval.
