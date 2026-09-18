# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```powershell
pip install -e ".[dev]"
copy .env.example .env
# Fill in ANTHROPIC_API_KEY; set REPO_URL to avoid passing --repo-url each time
```

## Commands

```powershell
# Run analysis (--repo-url is always required unless --local-path is used)
python main.py --repo-url https://github.com/<owner>/<repo>

# Analyze a directory already on disk instead of cloning (e.g. inside CI,
# after actions/checkout) -- see action.yml, which uses exactly this
python main.py --local-path C:\path\to\checked-out\repo

# Cheap smoke test (analyze a small subset of files)
python main.py --repo-url https://github.com/<owner>/<repo> --max-files 15

# Force re-clone the repo or bypass the LLM cache
python main.py --repo-url ... --refresh-repo --refresh-cache

# Override the report's ROI-vs-manual-review assumption (default: 200 LOC/hour at $75/hour)
python main.py --repo-url ... --review-loc-per-hour 150 --reviewer-hourly-rate 100

# Full flag reference
python main.py --help
```

```powershell
pytest              # unit tests — fully offline, no API calls
ruff check .        # lint
mypy src            # type-check (strict mode)
```

To run a single test file: `pytest tests/test_cache.py -v`

## Architecture

The pipeline is a strict linear sequence with no backwards edges. Each stage has a single file:

```
repo_fetcher.py     → shallow git clone of target repo (skipped entirely if --local-path is given)
java_parser.py      → AST parse of all .java files → ParsedClass / ParsedMethod (+ imports, class line bounds)
complexity.py       → heuristic LOC + cyclomatic per method → ComplexityMetrics
security_scanner.py → regex-based hardcoded-secret / SQL-concat / empty-catch checks → SecurityFinding
dependency_graph.py → import-based internal class-to-class edges → DependencyEdge
churn.py            → git log-derived commit count + last-modified per file → ChurnMetrics
chunker.py          → group classes into token-bounded batches → ClassBatch
llm_client.py       → LangChain ChatAnthropic calls with structured output
cache.py            → content-hash cache for LLM results across runs
pipeline.py         → orchestrates all stages, owns graceful-degradation policy
report_generator.py → renders ProjectAnalysis → HTML (Jinja2 autoescape)
```

All cross-module contracts are Pydantic models in `schemas.py` — never raw dicts. The stages produce: `ParseResult` → `ComplexityMetrics`/`SecurityFinding`/`DependencyEdge`/`ChurnMetrics` (all free, deterministic) → `ClassDescription` (via LLM) → `ProjectAnalysis` (the final deliverable, which also carries a `hotspots` ranking of `churn x complexity`).

**Two-stage cost split**: static analysis (java_parser, complexity, security_scanner, dependency_graph, churn) runs free; the LLM is called only for semantic descriptions (what does this class *mean*). Raw source is never sent to the LLM — only condensed signature/annotation/complexity text produced by `chunker.render_class_for_prompt`.

**Free deterministic signals beyond complexity**: `security_scanner.py` flags hardcoded credential-like fields, SQL built via string concatenation, and empty catch blocks — same "documented heuristic" posture as `ComplexityMetrics`, not a certified static-analysis tool. `dependency_graph.py` builds internal-only class-to-class edges from import statements (external/JDK/framework imports never appear as an edge); it matches on simple class names, not fully-qualified/type-resolved references, so two unrelated classes sharing a name in different packages can produce a spurious edge. `churn.py` runs one `git log` pass per run; it re-anchors paths to `repo_root` (not necessarily the git top level) so churn keys line up with every other module's `file_path`, and degrades to an empty dict — never raises — if history isn't available (shallow clone, `--local-path` on a non-git directory, missing `git` binary). `--git-history-depth` (default 200) controls how much history `repo_fetcher.py` clones; ignored when `--local-path` is used, since no clone happens.

**Batching**: Classes are grouped by source directory first, then capped by `--batch-size` (default 6) and a real token ceiling (default 4,000 input tokens). Token counts come from the Anthropic SDK's `count_tokens` endpoint — never a character-count heuristic or tiktoken.

**Caching**: `cache.py` keys on `SHA-256(PROMPT_VERSION + rendered_class_text)`. A class whose source is unchanged costs zero tokens on re-runs. Bump `PROMPT_VERSION` in `cache.py` when the extraction prompt changes meaningfully.

**Output**: `pipeline.py` writes `output/analysis.json`, `output/analysis.schema.json`, and `output/report.html` — all derived from the single `ProjectAnalysis` Pydantic model, so JSON and HTML can never disagree.

**Report-enrichment metrics** (`RunMetadata`, schema `1.4`): beyond per-run cost/cache stats, `pipeline.py` computes repo-wide `total_lines_of_code`; `estimated_total_tokens` (raw repo, via `count_tokens`) vs. `estimated_llm_tokens` (condensed prompt text, same tokenizer) and the resulting `token_savings_pct`; a `security_findings_high/medium/low` severity breakdown; and a `estimated_manual_review_hours/cost_usd`/`estimated_cost_savings_usd` ROI estimate against manual review, driven by `--review-loc-per-hour`/`--reviewer-hourly-rate` (defaults 200 LOC/hour, $75/hour — see `config.py`). The two `count_tokens`-based figures are the only report content needing a live API connection independent of caching; a failure there is logged and degrades to `0` rather than failing the run (unlike `chunker._count_tokens`, whose failure must abort the run since every batch depends on it).

**Error containment**: `pipeline.py` is the sole owner of the degradation policy. A failed parse, failed LLM batch, or failed overview call is logged and skipped; the run continues. Classes with no LLM description get an explicit `"Description unavailable."` placeholder rather than being silently dropped, plus `analysis_status=failed` on the class and a `RunMetadata.classes_with_failed_analysis` count (report tile + card badge) so the placeholder is distinguishable from a real description.

## Key Design Constraints

- `config.py` enforces fail-fast before any network call: either `--repo-url`/`REPO_URL` or `--local-path`, plus `ANTHROPIC_API_KEY`, must be present or a `ConfigurationError` is raised.
- `mypy --strict` is enforced project-wide; the only exception is `javalang.*` (no stubs available).
- The test suite (`tests/`) never makes real API calls — `LLMClient` and the token counter are injected/mocked via `conftest.py`. CI runs with no `ANTHROPIC_API_KEY`. `test_churn.py` does exercise real local `git` commands against a throwaway repo in `tmp_path` — offline, but not mocked, since it's testing `git log` parsing itself.
- `ClassType` classification is a heuristic: Spring stereotype annotations first, package directory keywords as fallback. Unconventional layouts classify as `OTHER`.
- A class too large for a single batch is split across sub-batches by `chunker._split_oversized_class`; that class is not cached (a partial result would be stored under the whole-class key).
- `action.yml` at the repo root makes this tool a reusable composite GitHub Action (`uses: rajamohamedml/intellisource-ai@<ref>`). It runs `main.py --local-path $GITHUB_WORKSPACE`, caches `.cache/` via `actions/cache` across runs, and writes a job-summary table.
- Never hardcode a target repository URL in the tool — it stays repo-agnostic, supplied only via `--repo-url`/`REPO_URL` or `--local-path`.
- Business-value figures (token savings, ROI vs. manual review) must stay labeled estimates: keep their assumed inputs (`review_loc_per_hour_assumed`, `reviewer_hourly_rate_usd_assumed`) visible in both `RunMetadata` and the report caption, never presented as measured facts.
- `docs/setup_run_guide.html` and `docs/cost_cache_architecture_overview.html` walk through the pipeline stage-by-stage and the cost/cache architecture, respectively — check them before re-explaining either from scratch.
