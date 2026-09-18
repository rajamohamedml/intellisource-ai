# bugfix-10 thread

- Investigate: reproduced with a tokenizer that charges for the "\n\n" join between classes -- 3 classes each under the ceiling were batched together, assembled text measured 107 vs an 84-token ceiling. Root cause: `chunker._batch_group` decides flushes from an additive sum of per-class counts (chunker.py, `would_exceed`), never counting the assembled text.
- Architect direction: keep the O(n) additive fast path; add one verification `count_tokens` on the assembled prompt_text at flush time, re-split only if it overshoots.
- Fix: `_verified_batches` (bisect + re-verify). Single-class batches skip the extra count (text identical to what was already measured). `_split_oversized_class` needed no change: it already counts the exact rendered text of every prospective sub-batch, and flush renders that same text, so an extra verification call would be pure redundant cost. Only remaining overshoot there is one method alone exceeding the ceiling, which cannot be split further.
- Not covered: system-prompt + structured-output schema overhead is not part of `ClassBatch.prompt_text` and is still uncounted (issue text mentions it; architect scoped the check to prompt_text).
- Gotcha: the editable install points at the MAIN checkout's src, so pytest in the worktree silently tests main's code unless run with PYTHONPATH=src (porch's own `tests` check needs that too).
