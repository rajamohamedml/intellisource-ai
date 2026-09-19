"""LangChain-based LLM integration.

Two structured-output chains, both built on a single `ChatAnthropic`
instance via `with_structured_output`, so schema-conformant JSON is
guaranteed by the SDK/API rather than by prompt engineering alone:

  - `analyze_batch`     — per-class descriptions for one chunker.ClassBatch.
  - `generate_overview` — the single, project-wide summary, run once.

Every call's token usage is accumulated onto a shared `UsageTracker` so
`pipeline.py` can report and cost the whole run — cost is a first-class,
inspectable output of this tool, not a hidden side effect.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_anthropic.chat_models import convert_to_anthropic_tool
from pydantic import BaseModel, SecretStr

from intellisource_ai.exceptions import LLMExtractionError
from intellisource_ai.schemas import ClassBatchAnalysis, ProjectOverview

logger = logging.getLogger(__name__)

# Published per-million-token list pricing (USD), used only to produce an
# approximate cost estimate in the run summary. Actual billing may differ
# (promotional/negotiated rates, price changes) — see README limitations.
_PRICING_USD_PER_MILLION_TOKENS: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
}

BATCH_SYSTEM_PROMPT = (
    "You are analyzing Java classes from a Spring Boot codebase. For each class "
    "provided, write a concise, technically accurate description of its purpose "
    "and, for every method listed, a one-sentence description of what it does. "
    "Base your analysis strictly on the signatures, annotations, Javadoc, and "
    "complexity flags given -- do not invent behavior you cannot infer from them. "
    "Reproduce each class_name and method signature exactly as given, so they can "
    "be matched back to the source. If a class has no notable design pattern, "
    "security-relevant logic, or complexity concern, return an empty notable_aspects "
    "list rather than inventing one."
)

# The tool definition and forced tool choice `with_structured_output(
# ClassBatchAnalysis)` attaches to every batch request, rebuilt through
# LangChain's own converter so `chunker.build_batches` can hand the exact same
# payload to `count_tokens` and reserve its tokens from the batch ceiling. The
# field names and `description=` guidance text in `schemas.py` are part of
# this payload, so schema edits are counted automatically.
# `test_llm_client.py` asserts this still equals what the real chain sends.
BATCH_TOOL_DEFINITION: dict[str, Any] = {**convert_to_anthropic_tool(ClassBatchAnalysis)}
BATCH_TOOL_CHOICE: dict[str, Any] = {"type": "tool", "name": BATCH_TOOL_DEFINITION["name"]}

_OVERVIEW_SYSTEM_PROMPT = (
    "You are summarizing a software project for a technical audience, given its "
    "README, its build dependencies, and a list of its classes' one-line purposes. "
    "Produce a clear, accurate high-level overview grounded strictly in the "
    "material given -- do not speculate about functionality not evidenced there."
)


@dataclass
class UsageTracker:
    """Accumulates token usage across every LLM call made during one run."""

    model: str
    calls_made: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def record(self, input_tokens: int, output_tokens: int) -> None:
        """Add one call's usage to the running totals."""
        self.calls_made += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    @property
    def estimated_cost_usd(self) -> float | None:
        """Approximate USD cost of every call recorded so far, or `None` when
        `self.model` has no known published pricing -- never a guessed
        substitute rate, which would look plausible but be wrong.
        """
        pricing = _PRICING_USD_PER_MILLION_TOKENS.get(self.model)
        if pricing is None:
            return None
        input_price, output_price = pricing
        return (self.input_tokens / 1_000_000) * input_price + (self.output_tokens / 1_000_000) * output_price


def _decode_stringified_json(value: Any) -> Any:
    """Recursively decode any string field that is itself JSON-encoded.

    Occasionally the model emits a list/object field as an escaped JSON
    string (e.g. `"classes": "[{...}]"`) instead of native nested JSON --
    seen on batches with a large number of methods, likely a size-pressure
    quirk of tool-call argument generation rather than a malformed
    response. Only strings that actually parse as JSON are touched; plain
    text fields (descriptions, etc.) pass through unchanged.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in "[{":
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return value
            return _decode_stringified_json(decoded)
        return value
    if isinstance(value, list):
        return [_decode_stringified_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _decode_stringified_json(item) for key, item in value.items()}
    return value


def _recover_stringified_fields(raw_message: Any, schema: type[BaseModel]) -> Any:
    """Best-effort repair for the double-encoded-field quirk described in
    `_decode_stringified_json`, tried only after normal structured-output
    parsing has already failed. Walks the raw tool call(s) on the
    underlying `AIMessage`, decodes any stringified list/object fields, and
    re-validates against `schema`. Returns `None` if no tool call recovers
    cleanly, so the caller can fall back to raising `LLMExtractionError`.
    """
    tool_calls = getattr(raw_message, "tool_calls", None) or []
    for call in tool_calls:
        args = call.get("args") if isinstance(call, dict) else None
        if not isinstance(args, dict):
            continue
        try:
            return schema.model_validate(_decode_stringified_json(args))
        except Exception:
            continue
    return None


class LLMClient:
    """Wraps a LangChain `ChatAnthropic` instance with the two
    structured-output extraction operations this pipeline needs.
    """

    def __init__(self, *, api_key: str, model: str, usage_tracker: UsageTracker) -> None:
        # max_retries covers transient 429/5xx failures automatically (SDK
        # default backoff); anything that still fails is surfaced as
        # LLMExtractionError by `_invoke` below, not raised raw.
        # Note: ChatAnthropic's pydantic fields are `model`/`anthropic_api_key`,
        # but their declared aliases -- `model_name`/`api_key` -- are what its
        # constructor actually expects.
        self._chat = ChatAnthropic(
            model_name=model, api_key=SecretStr(api_key), timeout=120.0, max_retries=2, stop=None
        )
        self._batch_chain = self._chat.with_structured_output(ClassBatchAnalysis, include_raw=True)
        self._overview_chain = self._chat.with_structured_output(ProjectOverview, include_raw=True)
        self._usage = usage_tracker

    def analyze_batch(self, prompt_text: str) -> ClassBatchAnalysis:
        """Send one batch of condensed class text to the LLM and return its
        structured per-class analysis.

        Raises:
            LLMExtractionError: if the call fails after the SDK's own
                retries, or the response fails schema validation.
        """
        messages = [("system", BATCH_SYSTEM_PROMPT), ("human", prompt_text)]
        result: ClassBatchAnalysis = self._invoke(self._batch_chain, messages, ClassBatchAnalysis)
        return result

    def generate_overview(self, prompt_text: str) -> ProjectOverview:
        """Produce the single, project-wide overview from README content,
        parsed build dependencies, and aggregated per-class one-line purposes.
        """
        messages = [("system", _OVERVIEW_SYSTEM_PROMPT), ("human", prompt_text)]
        result: ProjectOverview = self._invoke(self._overview_chain, messages, ProjectOverview)
        return result

    def _invoke(self, chain: Any, messages: list[tuple[str, str]], schema: type[BaseModel]) -> Any:
        try:
            result = chain.invoke(messages)
        except Exception as exc:  # SDK-level failure: network, auth, retries exhausted
            raise LLMExtractionError(f"LLM call failed: {exc}") from exc

        raw_message = result.get("raw")
        usage = getattr(raw_message, "usage_metadata", None) if raw_message is not None else None
        if usage:
            self._usage.record(usage.get("input_tokens", 0), usage.get("output_tokens", 0))

        parsed = result.get("parsed")
        if parsed is None:
            parsed = _recover_stringified_fields(raw_message, schema)
        if parsed is None:
            raise LLMExtractionError(
                f"LLM response failed schema validation: {result.get('parsing_error')}"
            )
        return parsed
