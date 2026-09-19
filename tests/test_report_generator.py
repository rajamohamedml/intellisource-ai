"""Tests for report_generator.py -- valid HTML output and autoescaping of
LLM-generated text (the XSS-safety guarantee described in the plan's
"Best Practices Incorporated" table).
"""

from __future__ import annotations

from intellisource_ai.report_generator import render_report
from intellisource_ai.schemas import (
    ClassAnalysis,
    ClassType,
    MethodAnalysis,
    ProjectAnalysis,
    ProjectOverview,
    RestEndpoint,
    RunMetadata,
)


def _sample_analysis(description: str) -> ProjectAnalysis:
    return ProjectAnalysis(
        project=ProjectOverview(
            name="Sample Project",
            description="A sample project for testing.",
            tech_stack=["Spring Boot", "Java"],
            architecture_summary="Layered architecture.",
            main_modules=["catalog"],
        ),
        classes=[
            ClassAnalysis(
                file_path="src/main/java/pkg/services/catalog/controller/WidgetController.java",
                package="pkg.services.catalog.controller",
                class_name="WidgetController",
                class_type=ClassType.CONTROLLER,
                description=description,
                rest_endpoints=[RestEndpoint(http_method="GET", path="/widgets")],
                methods=[
                    MethodAnalysis(
                        signature="Widget getWidget()",
                        description="Returns a widget.",
                        loc=5,
                        cyclomatic_estimate=1,
                        high_complexity=False,
                    ),
                    MethodAnalysis(
                        signature="void complexOp()",
                        description="Does something complex.",
                        loc=50,
                        cyclomatic_estimate=15,
                        high_complexity=True,
                    ),
                ],
                notable_aspects=["Uses the builder pattern."],
            )
        ],
        metadata=RunMetadata(
            generated_at="2026-01-01T00:00:00Z",
            model_used="claude-haiku-4-5",
            total_files_parsed=1,
            parse_errors=[],
            llm_calls_made=1,
            llm_calls_cached=0,
            total_input_tokens=100,
            total_output_tokens=50,
            estimated_cost_usd=0.0007,
        ),
    )


def test_renders_well_formed_html_with_expected_content() -> None:
    analysis = _sample_analysis("A controller for widgets.")

    html = render_report(analysis)

    assert html.startswith("<!DOCTYPE html>")
    assert "Sample Project" in html
    assert "WidgetController" in html
    assert "GET /widgets" in html
    assert "HIGH COMPLEXITY" in html
    assert "catalog" in html  # module-grouping heading, derived from the file path


def test_llm_generated_text_is_escaped_not_executed() -> None:
    dangerous_description = "A widget. <script>alert('xss')</script>"
    analysis = _sample_analysis(dangerous_description)

    html = render_report(analysis)

    assert "<script>alert('xss')</script>" not in html
    assert "&lt;script&gt;" in html


def test_unknown_pricing_renders_na_not_a_dollar_figure() -> None:
    analysis = _sample_analysis("A controller for widgets.")
    analysis.metadata.estimated_cost_usd = None
    analysis.metadata.estimated_cost_savings_usd = None

    html = render_report(analysis)

    assert "Pricing unavailable for model" in html
    assert "$0.0000" not in html


def test_known_pricing_renders_run_cost_as_incremental() -> None:
    html = render_report(_sample_analysis("A controller for widgets."))

    assert "$0.0007" in html
    assert "Incremental cost of this run only" in html
