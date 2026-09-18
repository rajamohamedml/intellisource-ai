"""Tests for pipeline.py's degradation policy: a class the LLM never
described (failed batch, or omitted from an otherwise-successful response)
must be flagged as `AnalysisStatus.FAILED` and counted, not silently
indistinguishable from a real description. Fully offline -- the LLM client
is a stub.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest

from intellisource_ai.chunker import ClassBatch
from intellisource_ai.exceptions import LLMExtractionError
from intellisource_ai.java_parser import parse_source_tree
from intellisource_ai.pipeline import (
    ClassKey,
    _assemble_classes,
    _count_failed_analyses,
    _process_batch,
)
from intellisource_ai.report_generator import render_report
from intellisource_ai.schemas import (
    AnalysisStatus,
    ClassAnalysis,
    ClassBatchAnalysis,
    ClassDescription,
    ParsedClass,
    ProjectAnalysis,
    ProjectOverview,
    RunMetadata,
)
from tests.conftest import SAMPLE_CONTROLLER_JAVA, SAMPLE_NESTED_DTO_JAVA


class _StubLLMClient:
    """Stands in for `LLMClient.analyze_batch`: either raises, or returns a
    canned response.
    """

    def __init__(self, response: ClassBatchAnalysis | None = None) -> None:
        self._response = response

    def analyze_batch(self, prompt_text: str) -> ClassBatchAnalysis:
        if self._response is None:
            raise LLMExtractionError("simulated LLM failure")
        return self._response


@pytest.fixture
def parsed_classes(tmp_path: Path) -> list[ParsedClass]:
    controller_dir = tmp_path / "src/main/java/com/example/app/services/catalog/controller"
    dto_dir = tmp_path / "src/main/java/com/example/app/services/catalog/domain/dto"
    controller_dir.mkdir(parents=True)
    dto_dir.mkdir(parents=True)
    (controller_dir / "ActorController.java").write_text(SAMPLE_CONTROLLER_JAVA)
    (dto_dir / "ActorDto.java").write_text(SAMPLE_NESTED_DTO_JAVA)
    return parse_source_tree(tmp_path, include_tests=False, max_files=None).classes


def _describe(fragments: dict[ClassKey, list[ClassDescription]]) -> dict[ClassKey, ClassDescription]:
    return {key: class_fragments[0] for key, class_fragments in fragments.items()}


def _assemble(
    classes: list[ParsedClass], descriptions: dict[ClassKey, ClassDescription]
) -> list[ClassAnalysis]:
    return _assemble_classes(classes, {}, descriptions, {}, {}, {})


def test_failed_batch_marks_every_class_failed_and_counts_them(parsed_classes: list[ParsedClass]) -> None:
    fragments: dict[ClassKey, list[ClassDescription]] = defaultdict(list)
    batch = ClassBatch(classes=parsed_classes, prompt_text="unused")

    _process_batch(batch, _StubLLMClient(response=None), fragments)  # type: ignore[arg-type]
    analyzed = _assemble(parsed_classes, _describe(fragments))

    assert len(analyzed) == len(parsed_classes) > 1
    assert all(cls.analysis_status is AnalysisStatus.FAILED for cls in analyzed)
    assert all(cls.description == "Description unavailable." for cls in analyzed)
    assert _count_failed_analyses(analyzed) == len(parsed_classes)


def test_class_omitted_from_llm_response_is_failed_but_described_siblings_are_not(
    parsed_classes: list[ParsedClass],
) -> None:
    controller = next(cls for cls in parsed_classes if cls.class_name == "ActorController")
    response = ClassBatchAnalysis(
        classes=[ClassDescription(class_name="ActorController", description="Serves actor endpoints.")]
    )
    fragments: dict[ClassKey, list[ClassDescription]] = defaultdict(list)

    _process_batch(
        ClassBatch(classes=parsed_classes, prompt_text="unused"),
        _StubLLMClient(response),  # type: ignore[arg-type]
        fragments,
    )
    analyzed = _assemble(parsed_classes, _describe(fragments))

    by_name = {cls.class_name: cls for cls in analyzed}
    assert by_name[controller.class_name].analysis_status is AnalysisStatus.DESCRIBED
    omitted = [cls for cls in analyzed if cls.class_name != controller.class_name]
    assert omitted and all(cls.analysis_status is AnalysisStatus.FAILED for cls in omitted)
    assert _count_failed_analyses(analyzed) == len(omitted)


def test_class_with_no_notable_aspects_is_described_not_failed(parsed_classes: list[ParsedClass]) -> None:
    response = ClassBatchAnalysis(
        classes=[
            ClassDescription(class_name=cls.class_name, description="Terse.", notable_aspects=[])
            for cls in parsed_classes
        ]
    )
    fragments: dict[ClassKey, list[ClassDescription]] = defaultdict(list)

    _process_batch(
        ClassBatch(classes=parsed_classes, prompt_text="unused"),
        _StubLLMClient(response),  # type: ignore[arg-type]
        fragments,
    )
    analyzed = _assemble(parsed_classes, _describe(fragments))

    assert all(cls.analysis_status is AnalysisStatus.DESCRIBED for cls in analyzed)
    assert _count_failed_analyses(analyzed) == 0


def test_report_surfaces_failed_analysis_tile_and_badge(parsed_classes: list[ParsedClass]) -> None:
    analyzed = _assemble(parsed_classes, {})
    analysis = ProjectAnalysis(
        project=ProjectOverview(name="P", description="d", architecture_summary="a"),
        classes=analyzed,
        metadata=RunMetadata(
            generated_at="2026-01-01T00:00:00+00:00",
            model_used="m",
            total_files_parsed=2,
            llm_calls_made=1,
            llm_calls_cached=0,
            total_input_tokens=1,
            total_output_tokens=1,
            estimated_cost_usd=0.0,
            classes_with_failed_analysis=_count_failed_analyses(analyzed),
        ),
    )

    html = render_report(analysis)

    assert "Classes With Failed Analysis" in html
    assert "analysis failed" in html
