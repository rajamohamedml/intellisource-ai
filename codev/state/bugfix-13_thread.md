# bugfix-13 thread

- Scope narrowed by architect: cross-model cache misattribution already fixed in #7 (cache key includes model). Remaining: (1) silent Haiku-pricing fallback, (2) cache-heavy re-run cost caption.
- Root cause (1): `UsageTracker.estimated_cost_usd` used `_DEFAULT_PRICING` for any model not in the pricing table. Fix: return `None`; `RunMetadata.estimated_cost_usd` / `estimated_cost_savings_usd` become nullable (schema 1.4); report/log/action.yml show "pricing unavailable".
- (2): `llm_calls_cached` already exists, so no new field; only captions changed to say cost/savings are this run's incremental spend.
- Gotcha: editable install points at the main checkout's `src`, so plain `pytest` in this worktree tests the unfixed code. Run with `PYTHONPATH=src` (needed for `porch done` checks too).
- Committed `output/` artifacts were not regenerated (needs a live API run); they still show schema 1.3.
