# bugfix-7 thread log

## Issue #7: Cache key omits model ID, causing silent cross-model misattribution

### Investigate
Root cause confirmed by reading code (no reproduction script needed — the bug is
structural, visible directly in the source): `compute_cache_key()` in
`src/intellisource_ai/cache.py` hashed only `PROMPT_VERSION` + rendered class text.
`pipeline.py::_analyze_classes()` had `settings.model` in scope (used elsewhere in
the same function, at the `build_batches` call) but never passed it into the key.
Blast radius: one production call site (`pipeline.py:231`), one test file
(`tests/test_cache.py`). Well under BUGFIX's 300 LOC ceiling.

### Fix
- `compute_cache_key(rendered_class_text, model)` now hashes
  `f"{PROMPT_VERSION}:{model}:{rendered_class_text}"`.
- `pipeline.py` passes `settings.model` at the call site.
- Updated 3 existing test calls to the new signature; added
  `test_different_model_produces_different_keys_for_same_content` — verified via
  stash/apply round-trip that it fails without the fix (TypeError on the old
  1-arg signature) and passes with it.
- Commit `1839fc2`.

**Environment gotcha worth remembering**: this worktree shares the main repo's
`.venv`, whose editable install (`pip install -e .`) points at the *main
checkout's* `src/`, not this worktree's. Bare `pytest`/`mypy`/`ruff check .`
therefore silently validate the wrong copy of the code unless `PYTHONPATH` is
exported to point at this worktree's `src/` first. Confirmed by watching
`compute_cache_key`'s resolved `__file__` change between the two. Porch's
check runner inherits `process.env`, so exporting `PYTHONPATH` in the shell
before `porch check`/`porch done` is enough — no repo changes needed.

### PR phase
- PR #14 opened: `Fix #7: include model ID in LLM cache key`.
- Porch's default `fix`-phase checks (`npm run build` / `npm test`) don't apply
  to this Python repo — architect added a `porch.checks` override to the
  shared `.codev/config.json` (`build: ruff check . && mypy src`,
  `tests: pytest`).
- CMAP 3-way review hit two environment gaps unrelated to the code:
  - gemini: `agy` CLI not installed — architect confirmed this stays
    unavailable, not blocking.
  - codex: default model `gpt-5.6-sol` unsupported for this ChatGPT-authenticated
    account — architect fixed via `consult.models.codex: gpt-5.6-terra` in the
    shared config; re-run succeeded.
  - claude: APPROVE (HIGH). Flagged two real nits (stale cache-key formula in
    `CLAUDE.md`/`docs/cost_cache_architecture_overview.html`, and a test using
    a relative `Path` instead of `tmp_path`) — fixed and pushed in `e9e5a9b`.
  - codex (after re-run): REQUEST_CHANGES (HIGH), but the sole issue was that
    `e9e5a9b` didn't carry a `Fix #7:` prefix — a stylistic point about the
    review-response commit, not the actual fix commit (`1839fc2`, which does).
    Architect reviewed and called it non-blocking.
- Human approved the `pr` gate; merged via `gh pr merge --merge` (no
  `--delete-branch`, since checked out on the branch in this worktree).
  Merge commit `fe05689`.

### Status: merged, protocol complete.
