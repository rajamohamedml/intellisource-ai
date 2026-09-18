# bugfix-12 thread — Issue #12: RunMetadata has no visibility into failed batches / undescribed classes

## 2026-09-18 — investigate
- Reproduced with a stub LLM client raising `LLMExtractionError` through `_process_batch` -> `_assemble_classes`:
  every class gets `"Description unavailable."` and no `RunMetadata`/`ClassAnalysis` field records that.
- Root cause: `_process_batch` swallows the failure (log + return) and `_assemble_classes` applies the
  placeholder with no status. Same path covers a class the LLM omits from a successful response.

## 2026-09-18 — fix
- Added `AnalysisStatus` (described/failed), `ClassAnalysis.analysis_status`,
  `RunMetadata.classes_with_failed_analysis`; report tile + per-class "analysis failed" badge.
- Deliberately class-level only: a class described by the LLM but missing some *method*
  descriptions still shows per-method placeholders and stays `described`. Left out to stay in BUGFIX scope.
- No third "no notable aspects" status: that is already `notable_aspects == []` on a `described` class.
- SCHEMA_VERSION 1.3 -> 1.4: additive, defaulted fields; matches how 1.3 was bumped for prior RunMetadata additions.
- Gotcha: the editable install points at the MAIN checkout, so bare `pytest` in this worktree tests master's
  code. Use `PYTHONPATH=$PWD/src`. Verified regression tests fail without the fix (ImportError) and pass with it.
- Merged origin/master (bugfix-10 landed); only conflict was a CLAUDE.md paragraph — kept master's text, set schema to 1.4.
