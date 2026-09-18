# bugfix-9 thread

- investigate: reproduced -- pipeline._estimate_repo_tokens sent full raw .java/config text to messages.count_tokens (spy client captured it verbatim). Root cause: unconditional call in run_pipeline, contradicting the CLAUDE.md "raw source never sent" constraint.
- Architect direction (2026-09-18): replace with a local, labeled chars-per-token approximation (no opt-in flag, no live call for raw text).
- fix: _estimate_repo_tokens now = total chars / 3.5 (pipeline._ESTIMATED_CHARS_PER_TOKEN), no API. estimated_llm_tokens still a live count (condensed text is sent to the LLM anyway). Report tile captions, schema comments, README, CLAUDE.md, docs updated to say "estimate".
- Extra guard: _compute_token_savings_pct returns 0.0 when llm_tokens <= 0; otherwise a failed live condensed count (-> 0) would now show a bogus 100% saving because the raw baseline is always non-zero.
- Note: token_savings_pct now mixes a heuristic raw baseline with a live-tokenizer condensed count; labeled approximate everywhere. Did not bump RunMetadata schema version (field meanings intact).
- Gotcha: editable install points at the main checkout; run tests in the worktree with PYTHONPATH=src.
