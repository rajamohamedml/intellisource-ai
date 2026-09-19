"""Guards the "what is sent to the LLM" claim in CLAUDE.md against drifting from
what `pipeline._generate_overview` actually sends (README/build-file excerpts).
"""

from __future__ import annotations

from pathlib import Path

from intellisource_ai import pipeline

_CLAUDE_MD = (Path(__file__).resolve().parent.parent / "CLAUDE.md").read_text(encoding="utf-8")


def test_claude_md_does_not_claim_all_raw_source_is_withheld() -> None:
    # The unqualified claim is false: README/build-file excerpts are sent too.
    assert "Raw source is never sent to the LLM" not in _CLAUDE_MD


def test_claude_md_scopes_claim_to_java_class_source() -> None:
    assert "Raw *Java class* source is never sent to the LLM" in _CLAUDE_MD


def test_claude_md_documents_overview_excerpt_exception() -> None:
    assert "_generate_overview" in _CLAUDE_MD
    assert "README" in _CLAUDE_MD
    assert f"{pipeline._README_CHAR_LIMIT:,} chars" in _CLAUDE_MD
    assert f"{pipeline._BUILD_FILE_CHAR_LIMIT:,} chars" in _CLAUDE_MD


def test_presentation_deck_does_not_claim_all_raw_source_is_withheld() -> None:
    deck = (Path(__file__).resolve().parent.parent / "docs" / "presentation-deck.md").read_text(encoding="utf-8")
    assert "never raw source," not in deck
    assert "never raw Java source" in deck
