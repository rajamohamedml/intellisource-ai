# Agent Instructions for IntelliSource AI

## Purpose

This repository is a Python CLI tool that analyzes an external Java/Spring repository and extracts structured knowledge about its classes, methods, REST endpoints, complexity, deterministic security findings, an internal dependency graph, and git-churn-derived hotspots. It is repository-agnostic: the target repo is supplied at runtime via `--repo-url`/`REPO_URL` or `--local-path` (an already-checked-out directory, e.g. inside CI).

The structural engine currently supports Java/Spring Boot codebases only; Python, TypeScript, and JavaScript support is on the roadmap. Only the parsing stage (`java_parser.py`) is language-specific — everything downstream already operates on language-agnostic schemas.

## Key workflows

- Analyze a target repository:
  - `python main.py --repo-url https://github.com/<owner>/<repo>`
- Analyze a directory already on disk instead of cloning (e.g. inside CI, after `actions/checkout`):
  - `python main.py --local-path /path/to/checked-out/repo`
- Run a smoke test on a small subset:
  - `python main.py --repo-url https://github.com/<owner>/<repo> --max-files 15`
- Refresh clone or LLM cache:
  - `python main.py --repo-url ... --refresh-repo --refresh-cache`
- Clone more history so churn/hotspots reflect real activity (default 200 commits):
  - `python main.py --repo-url ... --git-history-depth 500`
- Override the report's ROI-vs-manual-review assumption (default: 200 LOC/hour at $75/hour):
  - `python main.py --repo-url ... --review-loc-per-hour 150 --reviewer-hourly-rate 100`
- Skip HTML report generation:
  - `python main.py --repo-url ... --no-html-report`

## Test and quality commands

- `pytest` — unit tests run fully offline; mocks replace real LLM/token calls. `test_churn.py` exercises real local `git` commands against a throwaway repo in `tmp_path` (offline, not mocked, since it's testing `git log` parsing itself).
- `ruff check .` — linting.
- `mypy src` — strict static typing checks.

## Project structure and responsibilities

- `main.py` — CLI entrypoint that parses args, resolves settings, configures logging, and runs the pipeline.
- `src/intellisource_ai/config.py` — configuration and env handling.
- `src/intellisource_ai/repo_fetcher.py` — clone of the target repository (depth controlled by `--git-history-depth`); skipped entirely when `--local-path` is given.
- `src/intellisource_ai/java_parser.py` — parse `.java` files into AST-derived class and method metadata, plus imports and class line bounds.
- `src/intellisource_ai/complexity.py` — computes LOC and heuristic cyclomatic complexity.
- `src/intellisource_ai/security_scanner.py` — regex-based checks for hardcoded credential-like fields, SQL built via string concatenation, and empty catch blocks. Free, deterministic, not a certified SAST tool.
- `src/intellisource_ai/dependency_graph.py` — builds an internal-only class-to-class dependency graph from import statements; external/JDK/framework imports never appear as an edge.
- `src/intellisource_ai/churn.py` — one `git log` pass per run, re-anchored to `repo_root`; degrades to an empty dict (never raises) if history isn't available.
- `src/intellisource_ai/chunker.py` — batches classes into token-safe prompt chunks.
- `src/intellisource_ai/llm_client.py` — orchestrates LangChain/Anthropic calls with structured output.
- `src/intellisource_ai/cache.py` — content-hash cache for LLM results; avoids repeated token cost for unchanged content.
- `src/intellisource_ai/report_generator.py` — renders the final HTML report from the Pydantic model.
- `src/intellisource_ai/schemas.py` — core Pydantic models and the single `ProjectAnalysis` schema.
- `src/intellisource_ai/pipeline.py` — end-to-end orchestration, graceful degradation, the `hotspots` ranking (`churn x complexity`), and the report-enrichment stats (repo LOC, raw-vs-condensed token counts/savings, security severity breakdown, ROI estimate).
- `action.yml` (repo root) — reusable composite GitHub Action wrapping this CLI (`uses: rajamohamedml/intellisource-ai@<ref>`); runs `main.py --local-path $GITHUB_WORKSPACE`, caches `.cache/` via `actions/cache`, writes a job-summary table.

## Important conventions for agents

- Preserve the two-stage design: free deterministic static analysis (parsing, complexity, security findings, dependency graph, churn) first, then LLM-based semantic description only for what static analysis can't determine.
- Do not hardcode a target repository URL in the tool.
- Either `--repo-url`/`REPO_URL` or `--local-path` is required; `ANTHROPIC_API_KEY` is required to run LLM calls.
- The pipeline is linear and should degrade gracefully: parse failures, LLM failures, or missing git history should be logged and skipped, not crash the whole run. The same applies to the report-enrichment token estimates (`pipeline._estimate_repo_tokens`/`_estimate_llm_tokens`) — they're optional display figures, not core analysis output, so a `count_tokens` failure degrades to `0` instead of aborting the run. `_estimate_repo_tokens` is a local chars-per-token approximation: raw repository source must never be sent to any API (including `count_tokens`), only the condensed prompt text.
- Business-value figures (token savings, ROI vs. manual review) must stay labeled estimates: keep their assumed inputs (`review_loc_per_hour_assumed`, `reviewer_hourly_rate_usd_assumed`) visible in both `RunMetadata` and the report caption, never presented as measured facts.
- The output model is authoritative: `output/analysis.json`, `output/analysis.schema.json`, and `output/report.html` are generated from the same `ProjectAnalysis` object.
- The test suite expects no real API calls. Use existing mocks from `tests/conftest.py` when working on tests.
- `pyproject.toml` is the source of truth for dependencies and tooling config.
- Avoid exposing secrets or API keys in code or docs.

## Useful doc references

- `README.md` — project overview, architecture, usage, limitations, and setup instructions.
- `CLAUDE.md` — additional guidance for Claude-style agents and command examples.
- `docs/setup_run_guide.html` — the full pipeline, stage by stage, with every terminal command.
- `docs/cost_cache_architecture_overview.html` — the cost/cache architecture, free vs. LLM-billed stages.

## When adding or updating code

- Keep the code structure modular and single-responsibility.
- Keep prompt/caching behavior aligned with `cache.py` and `chunker.py`.
- Respect the existing output contract: HTML and JSON must derive from the same schema model.
- If you add a new feature or behavior, update `README.md` or `AGENTS.md` with a concise note instead of duplicating full docs.
