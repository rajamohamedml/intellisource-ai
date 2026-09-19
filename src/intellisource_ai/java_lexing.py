"""Shared Java lexical helpers.

A neutral leaf module (no intra-package imports) so both `java_parser.py`
(stage 1) and `complexity.py` (stage 2) can depend on it without either
reaching into the other's internals.
"""

from __future__ import annotations


def strip_comments_and_literals(text: str) -> str:
    """Blank out string/char literals and comments so keyword scans over the
    result (e.g. the complexity branch count, or the parser's switch-label
    detection) don't pick up "if"/"for"/"switch"/etc. that merely appear
    inside a log message or a comment rather than real code.

    A small hand-rolled state machine rather than a regex, since correctly
    handling escapes inside strings and nested-looking `/*.../*.../*/`
    sequences is awkward to express as a single regex. Replaces matched
    characters with spaces (preserving newlines) rather than deleting them,
    so the result is the same length as the input and offsets/line
    structure still map onto the original source.
    """
    result: list[str] = []
    in_line_comment = False
    in_block_comment = False
    in_string = False
    in_char = False
    i = 0
    while i < len(text):
        two_chars = text[i : i + 2]
        char = text[i]

        if in_line_comment:
            if char == "\n":
                in_line_comment = False
                result.append(char)
            else:
                result.append(" ")
            i += 1
            continue
        if in_block_comment:
            if two_chars == "*/":
                in_block_comment = False
                result.append("  ")
                i += 2
            else:
                result.append(char if char == "\n" else " ")
                i += 1
            continue
        if in_string:
            if char == "\\":
                result.append("  ")
                i += 2
                continue
            if char == '"':
                in_string = False
            result.append(" ")
            i += 1
            continue
        if in_char:
            if char == "\\":
                result.append("  ")
                i += 2
                continue
            if char == "'":
                in_char = False
            result.append(" ")
            i += 1
            continue

        if two_chars == "//":
            in_line_comment = True
            result.append("  ")
            i += 2
            continue
        if two_chars == "/*":
            in_block_comment = True
            result.append("  ")
            i += 2
            continue
        if char == '"':
            in_string = True
            result.append(" ")
            i += 1
            continue
        if char == "'":
            in_char = True
            result.append(" ")
            i += 1
            continue

        result.append(char)
        i += 1

    return "".join(result)
