from __future__ import annotations

import ast
from pathlib import Path

from intellisource_ai.java_lexing import strip_comments_and_literals


def test_strips_string_char_and_comment_contents() -> None:
    source = 'if (x) { log("if for while"); c = \'"\'; } // switch\n/* case */ y();'

    masked = strip_comments_and_literals(source)

    assert "if (x)" in masked
    assert "y();" in masked
    for hidden in ("for", "while", "switch", "case"):
        assert hidden not in masked


def test_preserves_length_and_newlines() -> None:
    source = 'a = "x\\"y";\n/* line1\nline2 */\nb = 1; // tail\n'

    masked = strip_comments_and_literals(source)

    assert len(masked) == len(source)
    assert [i for i, c in enumerate(masked) if c == "\n"] == [
        i for i, c in enumerate(source) if c == "\n"
    ]


def test_handles_escaped_quote_in_string() -> None:
    masked = strip_comments_and_literals('s = "a\\"if";\nreal();')

    assert "if" not in masked
    assert "real();" in masked


def test_parser_and_complexity_do_not_import_each_other() -> None:
    """Pipeline stages must not have backwards edges: the shared masking
    helper lives in `java_lexing`, so neither `java_parser` (stage 1) nor
    `complexity` (stage 2) may import from the other."""
    package_dir = Path(__file__).resolve().parents[1] / "src" / "intellisource_ai"

    def imported_modules(filename: str) -> set[str]:
        tree = ast.parse((package_dir / filename).read_text(encoding="utf-8"))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
                modules.update(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
        return modules

    assert "intellisource_ai.complexity" not in imported_modules("java_parser.py")
    assert "intellisource_ai.java_parser" not in imported_modules("complexity.py")
