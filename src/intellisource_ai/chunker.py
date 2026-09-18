"""Groups parsed classes into small, token-bounded batches for LLM calls.

Batching several small classes into one call amortizes the fixed cost of a
request (system prompt, schema description) across all of them. A hard
token ceiling — verified against the real Anthropic tokenizer via
`client.messages.count_tokens`, never a character-count guess or
`tiktoken` (the wrong tokenizer for Claude) — keeps every call bounded and
cheap. The ceiling bounds each batch's condensed class text, verified by
re-counting the assembled text; it does not include the fixed system-prompt
and structured-output schema overhead added at dispatch time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from anthropic import Anthropic

from intellisource_ai.exceptions import LLMExtractionError
from intellisource_ai.schemas import AnnotationInfo, ComplexityMetrics, ParsedClass, ParsedMethod

logger = logging.getLogger(__name__)

# Keyed by (file_path, class_name, method.signature) -> that method's
# complexity metrics, so the batch prompt can flag high-complexity methods
# for the LLM without re-deriving anything it already computed for free.
ComplexityIndex = dict[tuple[str, str, str], ComplexityMetrics]


@dataclass(frozen=True)
class ClassBatch:
    """One group of classes (or, for an oversized class, a group of that
    class's own methods) sized to fit under the configured token ceiling.
    `classes` retains full `ParsedClass` objects so `pipeline.py` can merge
    the LLM's response back onto the original structural data.
    """

    classes: list[ParsedClass]
    prompt_text: str


def _render_annotations(annotations: list[AnnotationInfo]) -> str:
    return "".join(f"@{a.name}({', '.join(a.arguments)}) " for a in annotations)


def _render_method(method: ParsedMethod, complexity: ComplexityMetrics | None) -> str:
    complexity_note = ""
    if complexity is not None:
        flag = " [HIGH COMPLEXITY]" if complexity.high_complexity else ""
        complexity_note = f"  // loc={complexity.loc}, cyclomatic~={complexity.cyclomatic_estimate}{flag}"
    doc = f"\n    /** {method.javadoc} */" if method.javadoc else ""
    return f"{doc}\n    {_render_annotations(method.annotations)}{method.signature};{complexity_note}"


def render_class_for_prompt(
    cls: ParsedClass, complexity_index: ComplexityIndex, *, methods: list[ParsedMethod] | None = None
) -> str:
    """Render one class into the condensed, signatures-only text sent to
    the LLM — never the raw source — so tokens are spent on what the model
    needs (structure + docs + complexity flags), not syntax it would have
    to re-derive itself. Class name and method signatures are reproduced
    verbatim so the model's structured-output `class_name`/`signature`
    fields can be matched back exactly by `pipeline.py`.
    """
    method_list = methods if methods is not None else cls.methods
    doc = f"/** {cls.javadoc} */\n" if cls.javadoc else ""
    method_lines = "".join(
        _render_method(m, complexity_index.get((cls.file_path, cls.class_name, m.signature)))
        for m in method_list
    )
    return (
        f"{doc}{_render_annotations(cls.annotations)}class {cls.class_name} "
        f"(package: {cls.package}, type: {cls.class_type.value}, file: {cls.file_path}) {{{method_lines}\n}}"
    )


def _count_tokens(client: Anthropic, model: str, text: str) -> int:
    """Count tokens for `text` via the real Anthropic tokenizer.

    Raises:
        LLMExtractionError: on any SDK-level failure (bad API key, network
            error, rate limit). Unlike a single failed batch analysis --
            which only affects the classes in that one batch and is
            handled per-batch in `pipeline.py` -- a token-counting failure
            here would recur identically for every subsequent class, so it
            is deliberately allowed to propagate and fail the whole run
            with a clean error rather than attempting per-class recovery.
    """
    try:
        response = client.messages.count_tokens(model=model, messages=[{"role": "user", "content": text}])
    except Exception as exc:
        raise LLMExtractionError(f"Token counting failed: {exc}") from exc
    return response.input_tokens


def _split_oversized_class(
    cls: ParsedClass, complexity_index: ComplexityIndex, client: Anthropic, model: str, token_ceiling: int
) -> list[ClassBatch]:
    """Split one class's methods across multiple sub-batches when even the
    single class's condensed text exceeds the token ceiling on its own
    (e.g. a very large class with many methods). Every method is still
    analyzed — nothing is truncated or dropped, just spread across more
    calls than usual.
    """
    logger.warning(
        "Class %s.%s alone exceeds the %d-token batch ceiling; splitting its "
        "methods across multiple sub-batches instead of truncating.",
        cls.package,
        cls.class_name,
        token_ceiling,
    )
    sub_batches: list[ClassBatch] = []
    current_methods: list[ParsedMethod] = []

    def flush() -> None:
        if not current_methods:
            return
        partial = cls.model_copy(update={"methods": list(current_methods)})
        rendered = render_class_for_prompt(partial, complexity_index)
        sub_batches.append(ClassBatch(classes=[partial], prompt_text=rendered))

    for method in cls.methods:
        prospective_methods = current_methods + [method]
        prospective_text = render_class_for_prompt(cls, complexity_index, methods=prospective_methods)
        prospective_tokens = _count_tokens(client, model, prospective_text)

        if current_methods and prospective_tokens > token_ceiling:
            flush()
            current_methods = [method]
        else:
            current_methods = prospective_methods

    flush()
    return sub_batches


def _verified_batches(
    classes: list[ParsedClass],
    rendered_parts: list[str],
    client: Anthropic,
    model: str,
    token_ceiling: int,
) -> list[ClassBatch]:
    """Return `classes` as one batch, after confirming the assembled text
    really fits `token_ceiling`; bisect and re-verify each half if it doesn't.

    The additive per-class running total in `_batch_group` can undershoot
    (separators and shared-context token boundaries aren't additive), so this
    is the one exact check against the text that will actually be dispatched.
    A single-class batch is never re-counted: its text is exactly the text
    already measured (and found within the ceiling) when it was rendered.
    """
    prompt_text = "\n\n".join(rendered_parts)
    if len(classes) == 1 or _count_tokens(client, model, prompt_text) <= token_ceiling:
        return [ClassBatch(classes=list(classes), prompt_text=prompt_text)]

    logger.warning(
        "Assembled batch of %d classes exceeds the %d-token ceiling despite per-class counts "
        "fitting; re-splitting it.",
        len(classes),
        token_ceiling,
    )
    mid = len(classes) // 2
    left = _verified_batches(classes[:mid], rendered_parts[:mid], client, model, token_ceiling)
    right = _verified_batches(classes[mid:], rendered_parts[mid:], client, model, token_ceiling)
    return left + right


def _batch_group(
    classes: list[ParsedClass],
    complexity_index: ComplexityIndex,
    client: Anthropic,
    model: str,
    batch_size: int,
    token_ceiling: int,
) -> list[ClassBatch]:
    batches: list[ClassBatch] = []
    current_classes: list[ParsedClass] = []
    current_text_parts: list[str] = []
    current_token_estimate = 0

    def flush() -> None:
        nonlocal current_classes, current_text_parts, current_token_estimate
        if current_classes:
            batches.extend(
                _verified_batches(current_classes, current_text_parts, client, model, token_ceiling)
            )
        current_classes, current_text_parts, current_token_estimate = [], [], 0

    for cls in classes:
        rendered = render_class_for_prompt(cls, complexity_index)
        class_tokens = _count_tokens(client, model, rendered)

        if class_tokens > token_ceiling:
            flush()
            batches.extend(_split_oversized_class(cls, complexity_index, client, model, token_ceiling))
            continue

        # Running total is a sum of independently-measured per-class token
        # counts, not a re-measurement of the concatenated batch text on
        # every addition — a small approximation (shared-context token
        # boundaries aren't perfectly additive) traded for O(n) instead of
        # O(n^2) count_tokens calls. The batch_size cap below is a second,
        # independent safety margin, and `_verified_batches` re-counts each
        # multi-class batch's assembled text once (re-splitting in the rare
        # case it overshoots), so the ceiling is still enforced literally.
        would_exceed = current_token_estimate + class_tokens > token_ceiling
        if current_classes and (would_exceed or len(current_classes) >= batch_size):
            flush()

        current_classes.append(cls)
        current_text_parts.append(rendered)
        current_token_estimate += class_tokens

    flush()
    return batches


def build_batches(
    classes: list[ParsedClass],
    complexity_index: ComplexityIndex,
    *,
    anthropic_client: Anthropic,
    model: str,
    batch_size: int,
    token_ceiling: int,
) -> list[ClassBatch]:
    """Group `classes` into token-bounded batches for LLM analysis.

    Classes are grouped by source directory first (so a batch's classes
    share feature/layer context, e.g. all of `services/catalog/controller`)
    before being capped by `batch_size` and the real token ceiling.

    Args:
        classes: All parsed classes to batch.
        complexity_index: Precomputed complexity metrics, embedded in the
            prompt text as flags.
        anthropic_client: Raw Anthropic SDK client used only for
            `count_tokens` — a separate, lightweight concern from the
            LangChain `ChatAnthropic` chains used for the actual analysis.
        model: Model ID to count tokens against (tokenization is model-specific).
        batch_size: Maximum classes per batch.
        token_ceiling: Maximum input tokens for each batch's assembled class
            text, enforced via real token counts (excluding the fixed
            system-prompt/schema overhead added at dispatch time).
    """
    by_directory: dict[str, list[ParsedClass]] = {}
    for cls in classes:
        directory = "/".join(cls.file_path.split("/")[:-1])
        by_directory.setdefault(directory, []).append(cls)

    batches: list[ClassBatch] = []
    for directory in sorted(by_directory):
        batches.extend(
            _batch_group(
                by_directory[directory], complexity_index, anthropic_client, model, batch_size, token_ceiling
            )
        )

    logger.info("Grouped %d classes into %d LLM batch(es)", len(classes), len(batches))
    return batches
