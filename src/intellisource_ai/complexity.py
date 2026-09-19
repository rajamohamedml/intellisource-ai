"""Deterministic complexity metrics for parsed methods.

Everything here is pure computation over already-parsed source text — no
LLM calls, no network. Kept in its own module so the "free" analysis (this
plus `java_parser.py`) stays clearly separated from the "paid" analysis in
`llm_client.py`.
"""

from __future__ import annotations

import re

from intellisource_ai.java_lexing import strip_comments_and_literals
from intellisource_ai.schemas import ComplexityMetrics, ParsedMethod

# 1 (baseline path) + one per branching construct found. This intentionally
# excludes the ternary operator `?:` — a naive `?` count would also match
# Java generic wildcards like `List<?>`, producing more false positives
# than the real ternaries it would catch.
_BRANCH_KEYWORDS = re.compile(r"\bif\b|\bfor\b|\bwhile\b|\bcase\b|\bcatch\b|&&|\|\|")

_DEFAULT_CYCLOMATIC_THRESHOLD = 10
_DEFAULT_LOC_THRESHOLD = 40


def compute_complexity(
    source_lines: list[str],
    method: ParsedMethod,
    *,
    cyclomatic_threshold: int = _DEFAULT_CYCLOMATIC_THRESHOLD,
    loc_threshold: int = _DEFAULT_LOC_THRESHOLD,
) -> ComplexityMetrics:
    """Compute LOC and an approximate cyclomatic complexity for one method.

    Args:
        source_lines: The full source file, split into lines (0-indexed),
            matching the 1-indexed `method.start_line`/`method.end_line`.
        method: The method whose body should be measured.
        cyclomatic_threshold: Estimate above which `high_complexity` is set.
        loc_threshold: LOC above which `high_complexity` is set regardless
            of the cyclomatic estimate — catches long-but-linear methods
            (e.g. a giant builder chain) that the branch count would miss.

    Returns:
        `ComplexityMetrics` whose `cyclomatic_estimate` is a heuristic, not
        a certified McCabe complexity score — see the field's docstring in
        `schemas.py` and the README's "Assumptions and Limitations".
    """
    body_lines = source_lines[method.start_line - 1 : method.end_line]
    loc = len(body_lines)
    body_text = strip_comments_and_literals("\n".join(body_lines))
    cyclomatic_estimate = 1 + len(_BRANCH_KEYWORDS.findall(body_text))
    high_complexity = cyclomatic_estimate > cyclomatic_threshold or loc > loc_threshold

    return ComplexityMetrics(
        loc=loc,
        cyclomatic_estimate=cyclomatic_estimate,
        high_complexity=high_complexity,
    )
