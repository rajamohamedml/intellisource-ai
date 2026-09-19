"""Pydantic data contracts used across every pipeline stage.

Every shape that crosses a module boundary is a Pydantic model, never a
raw dict — this gives us validation for free and makes the final JSON
deliverable's structure explicit and versioned (`SCHEMA_VERSION`).

The stages, in order, are:
    1. java_parser.py   -> ParseResult (ParsedClass / ParsedMethod)
    2. complexity.py    -> ComplexityMetrics (merged onto each ParsedMethod)
    3. llm_client.py    -> ClassBatchAnalysis / ProjectOverview (LLM structured output)
    4. pipeline.py       -> ProjectAnalysis (the final, assembled deliverable)
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.4"


class ClassType(StrEnum):
    """Convention-based classification of a class's architectural role,
    inferred from its package path and Spring annotations in java_parser.py.
    This is a heuristic, not an authoritative language-level concept — an
    unconventionally-organized codebase may be misclassified as OTHER.
    """

    CONTROLLER = "controller"
    SERVICE = "service"
    REPOSITORY = "repository"
    ENTITY = "entity"
    DTO = "dto"
    MAPPER = "mapper"
    ASSEMBLER = "assembler"
    CONFIG = "config"
    EXCEPTION = "exception"
    OTHER = "other"


# ---------------------------------------------------------------------------
# Stage 1 — structural data extracted by java_parser.py (zero LLM cost)
# ---------------------------------------------------------------------------


class AnnotationInfo(BaseModel):
    """A single Java annotation as it appeared on a class or method, e.g.
    `@GetMapping("/actors/{id}")` -> name="GetMapping", arguments=["/actors/{id}"].
    """

    name: str
    arguments: list[str] = Field(default_factory=list)


class RestEndpoint(BaseModel):
    """An HTTP route derived from a Spring mapping annotation. Only
    populated for controller methods — extracted from annotations directly,
    without any LLM involvement.
    """

    http_method: str
    path: str


class SecuritySeverity(StrEnum):
    """Severity of a `SecurityFinding`, independent of the LLM's own
    `notable_aspects` — these come from deterministic pattern matching in
    `security_scanner.py`, not model judgment.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SecurityFinding(BaseModel):
    """One deterministic security/quality signal flagged by
    `security_scanner.py` — regex-based pattern matching over source text,
    the same "documented heuristic" approach as `ComplexityMetrics`, not an
    LLM opinion. See that module for what each `category` actually detects.
    """

    category: str
    severity: SecuritySeverity
    message: str
    line: int | None = None


class ChurnMetrics(BaseModel):
    """Git-derived change frequency for one file, computed by `churn.py`.

    `commit_count` and `last_modified` reflect however much history is
    available in the local clone — a shallow clone (or a CI checkout with
    `fetch-depth: 1`) will under-report both. See `config.py`'s
    `--git-history-depth` for the clone-side knob that controls this.
    """

    commit_count: int
    last_modified: str | None = None


class DependencyEdge(BaseModel):
    """One directed "depends on" relationship between two classes in this
    repository, derived from import statements in `dependency_graph.py`.
    External (JDK/framework/library) imports are excluded — both ends of
    every edge are classes this run actually parsed.
    """

    from_class: str
    to_class: str


class ParsedMethod(BaseModel):
    """Structural facts about one method/constructor, extracted purely by
    parsing the AST. `start_line`/`end_line` are 1-indexed and are what
    complexity.py uses to slice the method body out of the source file.
    """

    name: str
    signature: str
    return_type: str
    parameters: list[str] = Field(default_factory=list)
    modifiers: list[str] = Field(default_factory=list)
    annotations: list[AnnotationInfo] = Field(default_factory=list)
    javadoc: str | None = None
    start_line: int
    end_line: int


class ParsedClass(BaseModel):
    """Structural facts about one class/interface/enum. One `.java` file can
    yield multiple `ParsedClass` entries — this codebase's DTOs nest several
    static classes inside a single outer file.

    `rest_endpoints` is populated purely from Spring mapping annotations
    (class-level `@RequestMapping` base path combined with method-level
    `@GetMapping`/etc.) — no LLM involvement, since this is derivable
    deterministically from the AST.
    """

    file_path: str
    package: str
    class_name: str
    class_type: ClassType
    javadoc: str | None = None
    annotations: list[AnnotationInfo] = Field(default_factory=list)
    methods: list[ParsedMethod] = Field(default_factory=list)
    rest_endpoints: list[RestEndpoint] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    start_line: int = 1
    end_line: int = 1


class ParseResult(BaseModel):
    """Everything `java_parser.py` extracted from one source tree, plus a
    record of any files it could not parse (never silently dropped).

    `files_discovered` is tracked separately from `len(classes)` because a
    file that parses successfully but declares zero types (e.g. package-info
    files) would otherwise be invisible in file-count reporting.
    """

    classes: list[ParsedClass] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)
    files_discovered: int = 0


# ---------------------------------------------------------------------------
# Stage 2 — complexity.py output (also zero LLM cost)
# ---------------------------------------------------------------------------


class ComplexityMetrics(BaseModel):
    """Deterministic complexity signal for one method.

    `cyclomatic_estimate` is a regex-based heuristic (1 + count of
    branching keywords/operators in the method body) — an approximation
    useful for flagging outliers, not a certified McCabe complexity score.
    See README "Assumptions and Limitations".
    """

    loc: int
    cyclomatic_estimate: int
    high_complexity: bool


# ---------------------------------------------------------------------------
# Stage 3 — LLM structured-output contracts (llm_client.py)
# ---------------------------------------------------------------------------
# Field `description=` text below is sent to the model as part of the tool
# schema via LangChain's `with_structured_output`, so it doubles as prompt
# guidance, not just documentation for humans reading this file.


class MethodDescription(BaseModel):
    """One method's LLM-written description, matched back to its signature."""

    signature: str = Field(description="Must exactly match one of the provided method signatures.")
    description: str = Field(description="One sentence explaining what the method does and why.")


class ClassDescription(BaseModel):
    """The LLM's semantic analysis of one class, matched back by class_name."""

    class_name: str = Field(description="Must exactly match one of the provided class names.")
    description: str = Field(description="A one-to-two sentence summary of the class's purpose.")
    methods: list[MethodDescription] = Field(default_factory=list)
    notable_aspects: list[str] = Field(
        default_factory=list,
        description="Design patterns, security-relevant logic, or other noteworthy "
        "characteristics of this class. Return an empty list if nothing stands out — "
        "do not invent findings.",
    )


class ClassBatchAnalysis(BaseModel):
    """Structured-output contract for one batched LLM call covering several classes."""

    classes: list[ClassDescription]


class ProjectOverview(BaseModel):
    """Structured-output contract for the single, project-wide overview LLM call."""

    name: str
    description: str = Field(
        description="1-2 paragraph summary of the project's purpose and functionality."
    )
    tech_stack: list[str] = Field(
        default_factory=list, description="Frameworks/libraries the project depends on."
    )
    architecture_summary: str = Field(
        description="How the codebase is organized (layers, modules, patterns)."
    )
    main_modules: list[str] = Field(
        default_factory=list,
        description="The project's main domain modules -- its business feature areas.",
    )


# ---------------------------------------------------------------------------
# Stage 4 — final assembled deliverable (pipeline.py)
# ---------------------------------------------------------------------------


class MethodAnalysis(BaseModel):
    """One method in the final report: structure + complexity + LLM description."""

    signature: str
    description: str
    loc: int
    cyclomatic_estimate: int
    high_complexity: bool


class ClassAnalysis(BaseModel):
    """One class in the final report."""

    file_path: str
    package: str
    class_name: str
    class_type: ClassType
    description: str
    rest_endpoints: list[RestEndpoint] = Field(default_factory=list)
    methods: list[MethodAnalysis] = Field(default_factory=list)
    notable_aspects: list[str] = Field(default_factory=list)
    security_findings: list[SecurityFinding] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    churn: ChurnMetrics | None = None


class RunMetadata(BaseModel):
    """Observability data about the run itself — cost, cache effectiveness,
    and any files that could not be parsed. Treating cost and correctness
    as first-class, inspectable outputs rather than hidden side effects.
    """

    generated_at: str
    model_used: str
    total_files_parsed: int
    parse_errors: list[str] = Field(default_factory=list)
    llm_calls_made: int
    llm_calls_cached: int
    total_input_tokens: int
    total_output_tokens: int
    # This run's incremental LLM spend only (classes served from cache add
    # nothing). None when model_used has no known pricing -- see
    # llm_client._PRICING_USD_PER_MILLION_TOKENS.
    estimated_cost_usd: float | None
    security_findings_total: int = 0
    total_lines_of_code: int = 0
    # Raw-repository size figures (every .java class file plus recognized
    # config files), independent of caching and of what this particular run
    # actually spent -- contrast with total_input_tokens/total_output_tokens
    # above, which are this run's actual, cache-aware LLM usage.
    estimated_total_tokens: int = 0
    llm_input_characters: int = 0
    estimated_llm_tokens: int = 0
    # Headline demo figure: percentage reduction from estimated_total_tokens
    # (raw repo) to estimated_llm_tokens (condensed extraction) -- how much
    # token cost this tool's structure-not-source approach saves.
    token_savings_pct: float = 0.0
    # Severity breakdown of security_findings_total, surfaced as its own
    # top-level tiles rather than requiring a reader to open Notable
    # Findings to see risk concentration.
    security_findings_high: int = 0
    security_findings_medium: int = 0
    security_findings_low: int = 0
    # ROI estimate vs. manual code review -- a labeled estimate built on
    # review_loc_per_hour_assumed / reviewer_hourly_rate_usd_assumed
    # (config.py's --review-loc-per-hour / --reviewer-hourly-rate), not a
    # measured figure. Carrying the assumed inputs alongside the derived
    # numbers keeps the estimate's basis visible in the report itself.
    review_loc_per_hour_assumed: int = 0
    reviewer_hourly_rate_usd_assumed: float = 0.0
    estimated_manual_review_hours: float = 0.0
    estimated_manual_review_cost_usd: float = 0.0
    estimated_cost_savings_usd: float | None = 0.0  # None when estimated_cost_usd is None


class ProjectAnalysis(BaseModel):
    """The complete, schema-versioned deliverable written to `analysis.json`
    and rendered into `report.html` by `report_generator.py`. Both files are
    derived from one instance of this model, so they can never disagree.
    """

    schema_version: str = SCHEMA_VERSION
    project: ProjectOverview
    classes: list[ClassAnalysis] = Field(default_factory=list)
    metadata: RunMetadata
    dependency_graph: list[DependencyEdge] = Field(default_factory=list)
    hotspots: list[str] = Field(
        default_factory=list,
        description="class_name values ranked by churn x complexity, highest risk first.",
    )

    @staticmethod
    def now_iso() -> str:
        """UTC timestamp helper used when constructing `RunMetadata.generated_at`."""
        return datetime.now(UTC).isoformat()
