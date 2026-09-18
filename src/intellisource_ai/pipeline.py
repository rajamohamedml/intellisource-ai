"""Orchestrates the full pipeline: fetch -> parse -> complexity -> chunk ->
LLM (cached) -> assemble -> write.

This module sequences the other modules and owns the error-containment
policy (a bad file, a failed batch, or a failed overview call is logged
and the run continues); it does not itself implement parsing, complexity,
chunking, or LLM logic — see the module each stage is named after.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from pathlib import Path

from anthropic import Anthropic

from intellisource_ai.cache import LLMCache, compute_cache_key
from intellisource_ai.chunker import ClassBatch, ComplexityIndex, build_batches, render_class_for_prompt
from intellisource_ai.churn import compute_churn
from intellisource_ai.complexity import compute_complexity
from intellisource_ai.config import Settings
from intellisource_ai.dependency_graph import build_dependency_graph, depends_on_by_class
from intellisource_ai.exceptions import LLMExtractionError
from intellisource_ai.java_parser import discover_java_files, parse_source_tree
from intellisource_ai.llm_client import LLMClient, UsageTracker
from intellisource_ai.repo_fetcher import fetch_repository
from intellisource_ai.report_generator import write_report
from intellisource_ai.schemas import (
    ChurnMetrics,
    ClassAnalysis,
    ClassDescription,
    MethodAnalysis,
    ParsedClass,
    ProjectAnalysis,
    ProjectOverview,
    RunMetadata,
    SecurityFinding,
    SecuritySeverity,
)
from intellisource_ai.security_scanner import scan_class

logger = logging.getLogger(__name__)

_README_CANDIDATES = ["README.md", "Readme.md", "readme.md"]
_BUILD_FILE_CANDIDATES = ["build.gradle.kts", "build.gradle", "pom.xml"]
_README_CHAR_LIMIT = 4000
_BUILD_FILE_CHAR_LIMIT = 3000
_HOTSPOT_COUNT = 8

# Spring Boot application config, in addition to the build files above --
# together, "every class and config file" a human reviewer would consider
# when sizing up the whole codebase for LLM ingestion (see _repo_size_stats).
_CONFIG_FILE_GLOBS = ("application*.properties", "application*.yml", "application*.yaml")
_EXCLUDED_CONFIG_DIR_NAMES = {"build", "target", ".git", "node_modules"}

# Per-call char budget for the condensed-text token count (see
# _count_tokens_for_text): comfortably under any model's context window even
# at a pessimistic ~2 chars/token, so a large codebase costs a handful of
# count_tokens calls rather than one per file.
_TOKEN_COUNT_CHUNK_CHARS = 500_000

# Documented approximation for source code (typical range ~3-4 chars/token
# across tokenizers). Used only by _estimate_repo_tokens, which deliberately
# stays local so raw repository source is never sent to any external API.
_ESTIMATED_CHARS_PER_TOKEN = 3.5

ClassKey = tuple[str, str]  # (file_path, class_name)


def run_pipeline(settings: Settings) -> ProjectAnalysis:
    """Execute the full pipeline end-to-end.

    As a side effect, writes `analysis.json`, `analysis.schema.json`, and
    (unless `--no-html-report` was passed) `report.html` under
    `settings.output_path`'s directory.

    Returns:
        The fully assembled `ProjectAnalysis`, matching what was written to disk.
    """
    if settings.local_path is not None:
        # Already-checked-out directory (e.g. a GitHub Action step running
        # after actions/checkout) -- skip cloning entirely.
        repo_root = settings.local_path
    else:
        assert settings.repo_url is not None  # guaranteed by config.resolve_settings
        repo_root = fetch_repository(
            settings.repo_url,
            settings.repo_ref,
            settings.repo_clone_path,
            refresh=settings.refresh_repo,
            depth=settings.git_history_depth,
        )

    parse_result = parse_source_tree(
        repo_root, include_tests=settings.include_tests, max_files=settings.max_files
    )
    complexity_index = _compute_all_complexity(repo_root, parse_result.classes)
    security_index = _scan_all_security(repo_root, parse_result.classes)
    dependency_edges = build_dependency_graph(parse_result.classes)
    depends_on_index = depends_on_by_class(dependency_edges)
    churn_index = compute_churn(repo_root)

    anthropic_client = Anthropic(api_key=settings.anthropic_api_key)
    usage_tracker = UsageTracker(model=settings.model)
    llm_client = LLMClient(
        api_key=settings.anthropic_api_key, model=settings.model, usage_tracker=usage_tracker
    )
    cache = LLMCache(settings.llm_cache_path, load_existing=not settings.refresh_cache)

    descriptions = _analyze_classes(
        parse_result.classes, complexity_index, anthropic_client, llm_client, cache, settings
    )
    cache.save()

    project_overview = _generate_overview(repo_root, parse_result.classes, descriptions, llm_client)
    analyzed_classes = _assemble_classes(
        parse_result.classes, complexity_index, descriptions, security_index, depends_on_index, churn_index
    )
    hotspots = _compute_hotspots(analyzed_classes)

    java_files = discover_java_files(
        repo_root, include_tests=settings.include_tests, max_files=settings.max_files
    )
    config_files = _discover_config_files(repo_root)
    total_lines_of_code = _compute_total_loc(java_files, config_files)
    estimated_total_tokens = _estimate_repo_tokens(java_files, config_files)
    llm_input_characters = _compute_llm_input_characters(parse_result.classes, complexity_index)
    estimated_llm_tokens = _estimate_llm_tokens(
        parse_result.classes, complexity_index, anthropic_client, settings.model
    )
    token_savings_pct = _compute_token_savings_pct(estimated_total_tokens, estimated_llm_tokens)

    severity_counts = _count_security_findings_by_severity(analyzed_classes)
    manual_hours, manual_cost_usd, cost_savings_usd = _estimate_manual_review_roi(
        total_lines_of_code,
        usage_tracker.estimated_cost_usd,
        settings.review_loc_per_hour,
        settings.reviewer_hourly_rate_usd,
    )

    metadata = RunMetadata(
        generated_at=ProjectAnalysis.now_iso(),
        model_used=settings.model,
        total_files_parsed=parse_result.files_discovered - len(parse_result.parse_errors),
        parse_errors=parse_result.parse_errors,
        llm_calls_made=usage_tracker.calls_made,
        llm_calls_cached=cache.hits,
        total_input_tokens=usage_tracker.input_tokens,
        total_output_tokens=usage_tracker.output_tokens,
        estimated_cost_usd=usage_tracker.estimated_cost_usd,
        security_findings_total=sum(len(cls.security_findings) for cls in analyzed_classes),
        total_lines_of_code=total_lines_of_code,
        estimated_total_tokens=estimated_total_tokens,
        llm_input_characters=llm_input_characters,
        estimated_llm_tokens=estimated_llm_tokens,
        token_savings_pct=token_savings_pct,
        security_findings_high=severity_counts[SecuritySeverity.HIGH],
        security_findings_medium=severity_counts[SecuritySeverity.MEDIUM],
        security_findings_low=severity_counts[SecuritySeverity.LOW],
        review_loc_per_hour_assumed=settings.review_loc_per_hour,
        reviewer_hourly_rate_usd_assumed=settings.reviewer_hourly_rate_usd,
        estimated_manual_review_hours=manual_hours,
        estimated_manual_review_cost_usd=manual_cost_usd,
        estimated_cost_savings_usd=cost_savings_usd,
    )

    analysis = ProjectAnalysis(
        project=project_overview,
        classes=analyzed_classes,
        metadata=metadata,
        dependency_graph=dependency_edges,
        hotspots=hotspots,
    )
    _write_outputs(analysis, settings)
    _log_summary(analysis)
    return analysis


def _compute_all_complexity(repo_root: Path, classes: list[ParsedClass]) -> ComplexityIndex:
    """Compute complexity metrics for every method in `classes`, reading
    each underlying source file exactly once even if it contains several
    classes (e.g. this codebase's multi-class DTO files).
    """
    index: ComplexityIndex = {}
    classes_by_file: dict[str, list[ParsedClass]] = defaultdict(list)
    for cls in classes:
        classes_by_file[cls.file_path].append(cls)

    for file_path, classes_in_file in classes_by_file.items():
        source_lines = (repo_root / file_path).read_text(encoding="utf-8", errors="replace").splitlines()
        for cls in classes_in_file:
            for method in cls.methods:
                index[(cls.file_path, cls.class_name, method.signature)] = compute_complexity(
                    source_lines, method
                )
    return index


def _scan_all_security(repo_root: Path, classes: list[ParsedClass]) -> dict[ClassKey, list[SecurityFinding]]:
    """Run `security_scanner.scan_class` for every class, reading each
    underlying source file exactly once (same pattern as
    `_compute_all_complexity`, for the same reason: multi-class files
    would otherwise be read once per class instead of once per file).
    """
    index: dict[ClassKey, list[SecurityFinding]] = {}
    classes_by_file: dict[str, list[ParsedClass]] = defaultdict(list)
    for cls in classes:
        classes_by_file[cls.file_path].append(cls)

    for file_path, classes_in_file in classes_by_file.items():
        source_lines = (repo_root / file_path).read_text(encoding="utf-8", errors="replace").splitlines()
        for cls in classes_in_file:
            index[(cls.file_path, cls.class_name)] = scan_class(source_lines, cls)
    return index


def _analyze_classes(
    classes: list[ParsedClass],
    complexity_index: ComplexityIndex,
    anthropic_client: Anthropic,
    llm_client: LLMClient,
    cache: LLMCache,
    settings: Settings,
) -> dict[ClassKey, ClassDescription]:
    """Analyze every class's semantics via the LLM, skipping any class
    whose exact rendered content is already cached from a prior run.
    """
    descriptions: dict[ClassKey, ClassDescription] = {}
    cache_keys: dict[ClassKey, str] = {}
    pending: list[ParsedClass] = []

    for cls in classes:
        key_tuple: ClassKey = (cls.file_path, cls.class_name)
        cache_key = compute_cache_key(render_class_for_prompt(cls, complexity_index))
        cache_keys[key_tuple] = cache_key
        cached = cache.get(cache_key)
        if cached is not None:
            descriptions[key_tuple] = cached
        else:
            pending.append(cls)

    logger.info(
        "%d class(es) served from cache, %d require LLM analysis", len(classes) - len(pending), len(pending)
    )

    fragments: dict[ClassKey, list[ClassDescription]] = defaultdict(list)
    if pending:
        batches = build_batches(
            pending,
            complexity_index,
            anthropic_client=anthropic_client,
            model=settings.model,
            batch_size=settings.batch_size,
            token_ceiling=settings.token_ceiling_per_batch,
        )
        for batch in batches:
            _process_batch(batch, llm_client, fragments)

    for key_tuple, class_fragments in fragments.items():
        merged = _merge_fragments(class_fragments)
        descriptions[key_tuple] = merged
        # A class only gets cached when it was analyzed in a single, whole
        # call. A class large enough to be split across sub-batches
        # (chunker._split_oversized_class) bypasses the cache rather than
        # caching a partial result under the whole-class key — a rare-case
        # simplification documented in the README.
        if len(class_fragments) == 1:
            cache.set(cache_keys[key_tuple], merged)

    return descriptions


def _process_batch(
    batch: ClassBatch, llm_client: LLMClient, fragments: dict[ClassKey, list[ClassDescription]]
) -> None:
    expected_by_name = {cls.class_name: cls for cls in batch.classes}
    try:
        result = llm_client.analyze_batch(batch.prompt_text)
    except LLMExtractionError as exc:
        # One failed batch must not sink the run — the classes it covered
        # simply fall back to a placeholder description in _assemble_classes.
        logger.error("Batch analysis failed for classes %s: %s", list(expected_by_name), exc)
        return

    for class_description in result.classes:
        source_class = expected_by_name.get(class_description.class_name)
        if source_class is None:
            logger.warning(
                "LLM returned class_name %r which was not in this batch; ignoring.",
                class_description.class_name,
            )
            continue
        fragments[(source_class.file_path, source_class.class_name)].append(class_description)


def _merge_fragments(fragments: list[ClassDescription]) -> ClassDescription:
    """Combine partial `ClassDescription`s for a class that was split
    across multiple sub-batches (see `chunker._split_oversized_class`)
    into one complete description covering all of its methods.
    """
    if len(fragments) == 1:
        return fragments[0]
    merged_methods = [method for fragment in fragments for method in fragment.methods]
    # dict.fromkeys deduplicates while preserving first-seen order, unlike set().
    merged_aspects = list(
        dict.fromkeys(aspect for fragment in fragments for aspect in fragment.notable_aspects)
    )
    return ClassDescription(
        class_name=fragments[0].class_name,
        description=fragments[0].description,
        methods=merged_methods,
        notable_aspects=merged_aspects,
    )


def _assemble_classes(
    classes: list[ParsedClass],
    complexity_index: ComplexityIndex,
    descriptions: dict[ClassKey, ClassDescription],
    security_index: dict[ClassKey, list[SecurityFinding]],
    depends_on_index: dict[str, list[str]],
    churn_index: dict[str, ChurnMetrics],
) -> list[ClassAnalysis]:
    """Merge structural data (java_parser), complexity metrics, LLM
    descriptions, and the three deterministic signals (security findings,
    the dependency graph, git churn) into the final `ClassAnalysis` list.
    A class or method the LLM never described (e.g. its batch failed) gets
    an explicit placeholder rather than being silently dropped.
    """
    assembled: list[ClassAnalysis] = []
    for cls in classes:
        description = descriptions.get((cls.file_path, cls.class_name))
        method_descriptions = {m.signature: m.description for m in description.methods} if description else {}

        methods: list[MethodAnalysis] = []
        for method in cls.methods:
            metrics = complexity_index.get((cls.file_path, cls.class_name, method.signature))
            methods.append(
                MethodAnalysis(
                    signature=method.signature,
                    description=method_descriptions.get(method.signature, "Description unavailable."),
                    loc=metrics.loc if metrics else 0,
                    cyclomatic_estimate=metrics.cyclomatic_estimate if metrics else 0,
                    high_complexity=metrics.high_complexity if metrics else False,
                )
            )

        assembled.append(
            ClassAnalysis(
                file_path=cls.file_path,
                package=cls.package,
                class_name=cls.class_name,
                class_type=cls.class_type,
                description=description.description if description else "Description unavailable.",
                rest_endpoints=cls.rest_endpoints,
                methods=methods,
                notable_aspects=description.notable_aspects if description else [],
                security_findings=security_index.get((cls.file_path, cls.class_name), []),
                depends_on=depends_on_index.get(cls.class_name, []),
                churn=churn_index.get(cls.file_path),
            )
        )
    return assembled


def _compute_hotspots(classes: list[ClassAnalysis]) -> list[str]:
    """Rank classes by churn x complexity -- "changed often AND hard to
    change safely" -- and return the top `_HOTSPOT_COUNT` class names,
    highest risk first. A class with no churn data (e.g. git history
    unavailable) scores 0 and is naturally excluded rather than favored.
    """
    scored = [
        (cls.class_name, cls.churn.commit_count * (1 + sum(1 for m in cls.methods if m.high_complexity)))
        for cls in classes
        if cls.churn is not None
    ]
    ranked = sorted(scored, key=lambda pair: (-pair[1], pair[0]))
    return [class_name for class_name, score in ranked if score > 0][:_HOTSPOT_COUNT]


def _count_security_findings_by_severity(classes: list[ClassAnalysis]) -> dict[SecuritySeverity, int]:
    """Tally `SecurityFinding`s by severity across every class, so the
    report can surface a High/Medium/Low breakdown as its own top-level
    tiles instead of requiring a reader to open Notable Findings.
    """
    counts = dict.fromkeys(SecuritySeverity, 0)
    for cls in classes:
        for finding in cls.security_findings:
            counts[finding.severity] += 1
    return counts


def _estimate_manual_review_roi(
    total_lines_of_code: int, actual_cost_usd: float, loc_per_hour: int, hourly_rate_usd: float
) -> tuple[float, float, float]:
    """Translate `total_lines_of_code` into a labeled ROI estimate against
    manual code review, using the caller-supplied throughput/rate
    assumptions (see config.py's `--review-loc-per-hour` /
    `--reviewer-hourly-rate`) -- an estimate, not a measured figure, which
    is why those assumed inputs are carried alongside it in `RunMetadata`.

    Returns (estimated_manual_hours, estimated_manual_cost_usd, estimated_cost_savings_usd).
    """
    if loc_per_hour <= 0:
        return 0.0, 0.0, 0.0
    hours = total_lines_of_code / loc_per_hour
    manual_cost = hours * hourly_rate_usd
    savings = max(0.0, manual_cost - actual_cost_usd)
    return round(hours, 1), round(manual_cost, 2), round(savings, 2)


def _discover_config_files(repo_root: Path) -> list[Path]:
    """Find build files (`_BUILD_FILE_CANDIDATES`) and Spring Boot
    application config files (`_CONFIG_FILE_GLOBS`) under `repo_root`.
    """
    found = [repo_root / name for name in _BUILD_FILE_CANDIDATES if (repo_root / name).is_file()]
    for pattern in _CONFIG_FILE_GLOBS:
        found.extend(
            f
            for f in sorted(repo_root.rglob(pattern))
            if not _EXCLUDED_CONFIG_DIR_NAMES.intersection(f.parts)
        )
    return found


def _compute_total_loc(java_files: list[Path], config_files: list[Path]) -> int:
    """Total physical line count across every `.java` source file and
    recognized config file this run considered -- a raw repo-size figure,
    distinct from the condensed per-method LOC `complexity.py` reports.
    """
    return sum(
        len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        for path in java_files + config_files
    )


def _count_tokens_for_text(text: str, anthropic_client: Anthropic, model: str) -> int:
    """Token count for `text` via the real Anthropic tokenizer -- never a
    character-count heuristic. `text` is chunked to a safe per-call size
    (`_TOKEN_COUNT_CHUNK_CHARS`) rather than counted all at once, so an
    entire repository's worth of text costs a handful of `count_tokens`
    calls rather than risking one oversized call.
    """
    if not text:
        return 0
    total_tokens = 0
    for offset in range(0, len(text), _TOKEN_COUNT_CHUNK_CHARS):
        chunk = text[offset : offset + _TOKEN_COUNT_CHUNK_CHARS]
        try:
            response = anthropic_client.messages.count_tokens(
                model=model, messages=[{"role": "user", "content": chunk}]
            )
        except Exception as exc:
            raise LLMExtractionError(f"Repository token estimation failed: {exc}") from exc
        total_tokens += response.input_tokens
    return total_tokens


def _estimate_repo_tokens(java_files: list[Path], config_files: list[Path]) -> int:
    """Approximate token count of the entire raw repository (every `.java`
    class file plus recognized config files): total characters divided by
    `_ESTIMATED_CHARS_PER_TOKEN`. A local heuristic, NOT a live-tokenizer
    count -- computed entirely offline so raw repository source is never
    transmitted to the Anthropic API (or anywhere else) for this
    report-only figure, and so it costs no API calls. It is the baseline
    for `token_savings_pct`, so treat that percentage as approximate too.
    """
    total_chars = sum(
        len(path.read_text(encoding="utf-8", errors="replace")) for path in java_files + config_files
    )
    return round(total_chars / _ESTIMATED_CHARS_PER_TOKEN)


def _compute_llm_input_characters(classes: list[ParsedClass], complexity_index: ComplexityIndex) -> int:
    """Total character count of the condensed, signatures-only prompt text
    `chunker.render_class_for_prompt` produces for every class in the
    codebase -- what actually gets sent to the LLM. An exact `len()` count,
    not an estimate -- no tokenizer needed to count characters.
    """
    return sum(len(render_class_for_prompt(cls, complexity_index)) for cls in classes)


def _estimate_llm_tokens(
    classes: list[ParsedClass], complexity_index: ComplexityIndex, anthropic_client: Anthropic, model: str
) -> int:
    """Token count of the condensed, signatures-only text this tool
    actually sends the LLM for every class in the codebase (the same text
    `_compute_llm_input_characters` measures in characters), via the real
    Anthropic tokenizer. Computed for the whole codebase regardless of this
    run's cache state, so it's a stable "what this tool's extraction costs
    by design" figure -- not this run's actual, cache-discounted spend
    (`RunMetadata.total_input_tokens`). Unlike `_estimate_repo_tokens`,
    this text is exactly what gets sent to the LLM anyway, so counting it
    live discloses nothing new. An optional report-enrichment stat: a
    failure is logged and degrades to 0 rather than aborting the run.
    """
    condensed_text = "".join(render_class_for_prompt(cls, complexity_index) for cls in classes)
    try:
        return _count_tokens_for_text(condensed_text, anthropic_client, model)
    except LLMExtractionError as exc:
        logger.error("Condensed LLM-input token estimation failed, reporting 0: %s", exc)
        return 0


def _compute_token_savings_pct(raw_tokens: int, llm_tokens: int) -> float:
    """Percentage reduction from the raw repository's approximate token
    count (`_estimate_repo_tokens`, a local heuristic) to the condensed
    text this tool actually sends the LLM (`_estimate_llm_tokens`, live
    tokenizer) -- the headline "how much token cost this tool saves by
    extracting structure instead of shipping raw source" figure, so an
    estimate. 0.0 if either side is unavailable (e.g. the live count
    failed and degraded to 0), rather than a misleading 100%.
    """
    if raw_tokens <= 0 or llm_tokens <= 0:
        return 0.0
    return round(100 * (1 - llm_tokens / raw_tokens), 1)


def _read_truncated(repo_root: Path, candidate_names: list[str], char_limit: int) -> str | None:
    """Read the first existing file among `candidate_names`, truncated to
    `char_limit` characters. Used for the one-off project overview prompt,
    where a bounded excerpt is enough context and keeps that single call cheap.
    """
    for name in candidate_names:
        candidate = repo_root / name
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8", errors="replace")[:char_limit]
    return None


def _generate_overview(
    repo_root: Path,
    classes: list[ParsedClass],
    descriptions: dict[ClassKey, ClassDescription],
    llm_client: LLMClient,
) -> ProjectOverview:
    """Produce the single, project-wide overview from a README excerpt, a
    build-file excerpt (the LLM infers the tech stack from it directly,
    rather than this tool hand-parsing Gradle/Maven dependency syntax --
    a deliberate simplification, see README), and every class's one-line
    purpose already generated in `_analyze_classes`.
    """
    readme_text = _read_truncated(repo_root, _README_CANDIDATES, _README_CHAR_LIMIT)
    build_text = _read_truncated(repo_root, _BUILD_FILE_CANDIDATES, _BUILD_FILE_CHAR_LIMIT)

    purposes = [
        f"- {cls.class_name} ({cls.class_type.value}): {description.description}"
        for cls in classes
        if (description := descriptions.get((cls.file_path, cls.class_name))) is not None
    ]

    prompt = (
        f"README:\n{readme_text or '(none found)'}\n\n"
        f"Build file (dependencies):\n{build_text or '(none found)'}\n\n"
        f"Class purposes:\n" + "\n".join(purposes)
    )

    try:
        return llm_client.generate_overview(prompt)
    except LLMExtractionError as exc:
        logger.error("Project overview generation failed: %s", exc)
        return ProjectOverview(
            name=repo_root.name,
            description="Overview generation failed -- see run logs for details.",
            tech_stack=[],
            architecture_summary="",
            main_modules=[],
        )


def _write_outputs(analysis: ProjectAnalysis, settings: Settings) -> None:
    """Write the JSON deliverable, its JSON Schema, and (unless disabled)
    the HTML report -- all three derived from the same `analysis` object.
    """
    settings.output_path.parent.mkdir(parents=True, exist_ok=True)
    settings.output_path.write_text(analysis.model_dump_json(indent=2), encoding="utf-8")
    settings.schema_output_path.write_text(
        json.dumps(ProjectAnalysis.model_json_schema(), indent=2), encoding="utf-8"
    )
    logger.info("Wrote %s and %s", settings.output_path, settings.schema_output_path)

    if settings.generate_html_report:
        write_report(analysis, settings.report_output_path)
        logger.info("Wrote %s", settings.report_output_path)


def _log_summary(analysis: ProjectAnalysis) -> None:
    m = analysis.metadata
    logger.info(
        "Done: %d file(s) parsed (%d parse error(s)), %d class(es) analyzed, "
        "%d LLM call(s) made / %d served from cache, %d input + %d output tokens, "
        "~$%.4f estimated cost",
        m.total_files_parsed,
        len(m.parse_errors),
        len(analysis.classes),
        m.llm_calls_made,
        m.llm_calls_cached,
        m.total_input_tokens,
        m.total_output_tokens,
        m.estimated_cost_usd,
    )
