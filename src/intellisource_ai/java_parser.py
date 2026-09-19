"""Static structural extraction from Java source files.

This is the "free" half of the pipeline's cost strategy: everything a
deterministic parser can tell us for certain — class/method signatures,
annotations, REST routes, Javadoc — is extracted here without spending a
single LLM token. Only the *semantic* description of what a class/method
does is deferred to the LLM (see `llm_client.py`).

Uses `javalang`, a pure-Python Java grammar parser. It does not cover every
corner of modern Java (e.g. `record`, sealed classes, pattern-matching
`switch`), so a per-file parse failure is expected on some codebases and is
handled as a first-class, non-fatal outcome (`ParseResult.parse_errors`)
rather than aborting the whole run.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import javalang
from javalang.tree import (
    ClassDeclaration,
    EnumDeclaration,
    InterfaceDeclaration,
)

from intellisource_ai.exceptions import JavaParseError
from intellisource_ai.java_lexing import strip_comments_and_literals
from intellisource_ai.schemas import (
    AnnotationInfo,
    ClassType,
    ParsedClass,
    ParsedMethod,
    ParseResult,
    RestEndpoint,
)

logger = logging.getLogger(__name__)

_TYPE_DECLARATIONS: tuple[type, ...] = (ClassDeclaration, InterfaceDeclaration, EnumDeclaration)

# Spring mapping annotations and the HTTP verb each implies. RequestMapping
# carries no fixed verb on its own — its verb (if any) is read from a
# `method = RequestMethod.X` argument instead.
_MAPPING_ANNOTATIONS: dict[str, str] = {
    "GetMapping": "GET",
    "PostMapping": "POST",
    "PutMapping": "PUT",
    "DeleteMapping": "DELETE",
    "PatchMapping": "PATCH",
    "RequestMapping": "REQUEST",
}

# Annotation name -> ClassType, checked before the directory-name fallback
# since an explicit Spring stereotype annotation is a stronger signal than
# a naming convention.
_ANNOTATION_TYPE_HINTS: dict[str, ClassType] = {
    "RestController": ClassType.CONTROLLER,
    "Controller": ClassType.CONTROLLER,
    "Service": ClassType.SERVICE,
    "Repository": ClassType.REPOSITORY,
    "Entity": ClassType.ENTITY,
    "Configuration": ClassType.CONFIG,
}

# Directory-name -> ClassType fallback, used when no annotation hint matched.
# Ordered by specificity where overlap is possible (e.g. check "repository"
# before a generic catch-all).
_PATH_TYPE_HINTS: tuple[tuple[str, ClassType], ...] = (
    ("controller", ClassType.CONTROLLER),
    ("service", ClassType.SERVICE),
    ("repository", ClassType.REPOSITORY),
    ("entity", ClassType.ENTITY),
    ("dto", ClassType.DTO),
    ("mapper", ClassType.MAPPER),
    ("assembler", ClassType.ASSEMBLER),
    ("config", ClassType.CONFIG),
    ("exception", ClassType.EXCEPTION),
)

_JAVADOC_PATTERN = re.compile(r"/\*\*(.*?)\*/", re.DOTALL)
_JAVADOC_LINE_NOISE = re.compile(r"^\s*\*\s?", re.MULTILINE)


def _find_type_declaration_index(source: str, type_name: str) -> int:
    """Locate the source offset of `class|interface|enum <TypeName>`.

    Used only to anchor the "nearest preceding Javadoc" search — a plain
    substring search for the type name would also match unrelated
    occurrences (references, other identifiers containing the same text),
    so this anchors on the declaration keyword with word boundaries.
    Returns 0 (start of file) if not found, which simply means no Javadoc
    will be associated.
    """
    match = re.search(rf"\b(?:class|interface|enum)\s+{re.escape(type_name)}\b", source)
    return match.start() if match else 0


def discover_java_files(repo_root: Path, *, include_tests: bool, max_files: int | None) -> list[Path]:
    """Find `.java` source files to analyze.

    Prefers the standard Maven/Gradle layout (`src/main/java`, optionally
    `src/test/java`); falls back to scanning the whole repository for any
    non-standard layout so the tool still works against other project
    structures. Common build-output directories are always excluded.

    Args:
        repo_root: Root of the cloned repository.
        include_tests: Whether to also include `src/test/java`.
        max_files: If set, caps the number of files returned (smoke-testing).
    """
    main_root = repo_root / "src" / "main" / "java"
    if main_root.is_dir():
        roots = [main_root]
        if include_tests:
            test_root = repo_root / "src" / "test" / "java"
            if test_root.is_dir():
                roots.append(test_root)
        files = [f for root in roots for f in sorted(root.rglob("*.java"))]
    else:
        logger.warning(
            "No src/main/java found under %s; falling back to a repo-wide *.java scan.",
            repo_root,
        )
        excluded_dir_names = {"build", "target", ".git", "node_modules"}
        files = [
            f
            for f in sorted(repo_root.rglob("*.java"))
            if not excluded_dir_names.intersection(f.parts)
        ]

    if max_files is not None:
        files = files[:max_files]
    return files


def _extract_javadoc_before(source: str, decl_start_index: int) -> str | None:
    """Find the nearest `/** ... */` block that immediately precedes a
    declaration, by searching backward from its source offset.

    `javalang` does not retain comments in its AST, so this is a best-effort
    regex heuristic, not a guarantee — a Javadoc block separated from its
    declaration by blank lines or annotations may occasionally be missed
    or mis-associated. Good enough for surfacing existing documentation to
    the LLM as extra context; not used for anything safety-critical.
    """
    preceding_text = source[:decl_start_index]
    matches = list(_JAVADOC_PATTERN.finditer(preceding_text))
    if not matches:
        return None
    last_match = matches[-1]
    # Reject stale matches: if there's a lot of non-whitespace code between
    # the comment and the declaration, it almost certainly documents
    # something else higher up in the file.
    gap = preceding_text[last_match.end():]
    if len(gap.strip()) > 200:
        return None
    text = _JAVADOC_LINE_NOISE.sub("", last_match.group(1))
    return text.strip() or None


def _stringify_annotation_value(value: Any) -> str:
    """Best-effort conversion of a javalang annotation-argument node into a
    plain string, e.g. a string Literal -> its text, a MemberReference like
    `RequestMethod.GET` -> "RequestMethod.GET". Falls back to `str(value)`
    for node shapes we don't specifically handle — annotation arguments are
    presentational metadata here (e.g. REST paths), not something we
    execute, so an imperfect fallback is an acceptable heuristic.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip('"')
    literal_value = getattr(value, "value", None)
    if isinstance(literal_value, str):
        return literal_value.strip('"')
    member = getattr(value, "member", None)
    if member is not None:
        qualifier = getattr(value, "qualifier", None)
        return f"{qualifier}.{member}" if qualifier else str(member)
    return str(value)


def _parse_annotation(annotation: Any) -> AnnotationInfo:
    """Convert one javalang `Annotation` node into our `AnnotationInfo` model."""
    element = annotation.element
    arguments: list[str] = []
    if element is None:
        pass
    elif isinstance(element, list):
        for pair in element:
            pair_value = getattr(pair, "value", pair)
            pair_name = getattr(pair, "name", None)
            stringified = _stringify_annotation_value(pair_value)
            arguments.append(f"{pair_name}={stringified}" if pair_name else stringified)
    else:
        arguments.append(_stringify_annotation_value(element))
    return AnnotationInfo(name=annotation.name, arguments=arguments)


def _infer_class_type(package: str, file_path: str, annotations: list[AnnotationInfo]) -> ClassType:
    """Classify a class's architectural role. Annotation-based Spring
    stereotypes are checked first (explicit signal); the directory path is
    the fallback (naming-convention signal). See `ClassType` docstring for
    why this is a heuristic, not an authoritative classification.
    """
    for annotation in annotations:
        hinted = _ANNOTATION_TYPE_HINTS.get(annotation.name)
        if hinted is not None:
            return hinted

    lowered_path = file_path.lower()
    for keyword, class_type in _PATH_TYPE_HINTS:
        if f"/{keyword}/" in lowered_path or lowered_path.startswith(f"{keyword}/"):
            return class_type
    return ClassType.OTHER


def _annotation_endpoint_path(annotation: AnnotationInfo) -> str:
    """Pull a bare URL path out of a mapping annotation's arguments,
    stripping a `value=`/`path=` prefix if the annotation used named args.
    """
    for arg in annotation.arguments:
        for prefix in ("value=", "path="):
            if arg.startswith(prefix):
                return arg[len(prefix):]
        if "=" not in arg:
            return arg
    return ""


def _extract_rest_endpoints(
    class_annotations: list[AnnotationInfo], method_annotations: list[AnnotationInfo]
) -> list[RestEndpoint]:
    """Combine a class-level `@RequestMapping` base path with method-level
    mapping annotations to produce full route paths, e.g. base "/actors" +
    method "/{id}" -> "/actors/{id}". Purely annotation-derived — no LLM.
    """
    base_path = ""
    for annotation in class_annotations:
        if annotation.name == "RequestMapping":
            base_path = _annotation_endpoint_path(annotation).rstrip("/")
            break

    endpoints: list[RestEndpoint] = []
    for annotation in method_annotations:
        http_method = _MAPPING_ANNOTATIONS.get(annotation.name)
        if http_method is None:
            continue
        method_path = _annotation_endpoint_path(annotation)
        if not method_path.startswith("/") and method_path:
            method_path = f"/{method_path}"
        full_path = f"{base_path}{method_path}" or "/"
        endpoints.append(RestEndpoint(http_method=http_method, path=full_path))
    return endpoints


def _compute_block_end_line(source_lines: list[str], start_line: int) -> int:
    """Determine the last line of a `{ ... }` block (a method body or a
    class/interface/enum body) by counting brace depth from its
    declaration onward.

    `javalang` records where a declaration *starts* but not where its body
    *ends*, so this scans forward, tracking whether we're inside a string,
    character literal, or comment (so a `{`/`}` inside e.g. a string
    argument isn't miscounted). This is a lightweight heuristic, not a full
    Java lexer — documented here since it's the least obvious piece of
    logic in this module. Abstract/interface methods with no body (a bare
    `;`) return their own start line.
    """
    depth = 0
    started = False
    in_block_comment = False
    in_string = False
    in_char = False

    for line_index in range(start_line - 1, len(source_lines)):
        line = source_lines[line_index]
        i = 0
        while i < len(line):
            two_chars = line[i : i + 2]
            if in_block_comment:
                if two_chars == "*/":
                    in_block_comment = False
                    i += 2
                    continue
                i += 1
                continue
            if in_string:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == '"':
                    in_string = False
                i += 1
                continue
            if in_char:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == "'":
                    in_char = False
                i += 1
                continue
            if two_chars == "//":
                # Rest of the line is a comment -- nothing left on it can
                # affect brace depth, so stop scanning this line entirely.
                break
            if two_chars == "/*":
                in_block_comment = True
                i += 2
                continue
            if line[i] == '"':
                in_string = True
            elif line[i] == "'":
                in_char = True
            elif line[i] == "{":
                depth += 1
                started = True
            elif line[i] == "}":
                depth -= 1
                if started and depth == 0:
                    return line_index + 1
            elif line[i] == ";" and not started:
                # Abstract/interface method declaration with no body.
                return line_index + 1
            i += 1

    # Fell off the end of the file without balancing braces — return the
    # last line rather than raising, so one odd method doesn't kill parsing
    # of the rest of the file.
    return len(source_lines)


def _offset_for_position(source_lines: list[str], line: int, column: int | None) -> int:
    """Approximate the character offset into `source` for a javalang
    (line, column) position (both 1-indexed).

    Assumes `\\n` line endings; a file using `\\r\\n` will be off by a small,
    harmless amount, since this offset only anchors a backward Javadoc
    search that already tolerates a 200-character gap — not something
    requiring byte-exact precision.
    """
    offset = sum(len(preceding_line) + 1 for preceding_line in source_lines[: line - 1])
    if column:
        offset += column - 1
    return offset


def _parse_method(node: Any, source_lines: list[str], source: str) -> ParsedMethod:
    parameters = [f"{p.type.name} {p.name}" for p in node.parameters]
    return_type = getattr(node, "return_type", None)
    return_type_name = return_type.name if return_type is not None else "void"
    start_line = node.position.line if node.position else 1
    column = node.position.column if node.position else None
    end_line = _compute_block_end_line(source_lines, start_line)
    signature = f"{return_type_name} {node.name}({', '.join(parameters)})"
    method_offset = _offset_for_position(source_lines, start_line, column)

    return ParsedMethod(
        name=node.name,
        signature=signature,
        return_type=return_type_name,
        parameters=parameters,
        modifiers=sorted(node.modifiers),
        annotations=[_parse_annotation(a) for a in node.annotations],
        javadoc=_extract_javadoc_before(source, method_offset),
        start_line=start_line,
        end_line=end_line,
    )


_SWITCH_KEYWORD = re.compile(r"(?<![\w$])switch(?![\w$])")


def _find_switch_body_open_braces(masked: str) -> set[int]:
    """Locate the opening `{` of every `switch (...) { ... }` body in
    `masked` (a same-length, string/comment-blanked copy of the source --
    see `java_lexing.strip_comments_and_literals` -- so a `switch` inside a
    string/comment, or a stray `{`/`}` inside one, can't cause a false
    match). Used to distinguish a switch's own top-level `case`/`default`
    labels from ones nested inside an unrelated block.
    """
    positions: set[int] = set()
    n = len(masked)
    for match in _SWITCH_KEYWORD.finditer(masked):
        paren_start = masked.find("(", match.end())
        if paren_start == -1:
            continue
        depth = 0
        j = paren_start
        while j < n:
            if masked[j] == "(":
                depth += 1
            elif masked[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        else:
            continue  # unbalanced parens -- malformed, skip rather than guess
        k = j + 1
        while k < n and masked[k] in " \t\r\n":
            k += 1
        if k < n and masked[k] == "{":
            positions.add(k)
    return positions


def _find_label_terminator(masked: str, start: int) -> int | None:
    """From just after a `case`/`default` keyword, scan forward (skipping
    over any parenthesized/bracketed label content, e.g. a record-pattern
    label) to find the label's terminator.

    Returns the offset of `->` if this is an arrow-style label (the thing
    this whole normalization exists to rewrite), or `None` if it's already
    colon-style or the file doesn't have the shape this function expects
    (in which case the caller leaves it untouched rather than guessing).
    """
    n = len(masked)
    depth = 0
    i = start
    while i < n:
        ch = masked[i]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0:
            if masked[i : i + 2] == "->":
                return i
            if ch in ":{};":
                return None
        i += 1
    return None


def _find_top_level_commas(masked: str, start: int, end: int) -> list[int]:
    """Offsets of the comma(s) separating a multi-label case
    (`case A, B ->`) within `masked[start:end]`, skipping any comma nested
    inside parens/brackets (e.g. a record-pattern label's own argument
    list). `javalang` predates multi-label cases entirely -- even in
    colon form -- so each one found here gets turned into its own
    `case`, achieving the same fallthrough semantics the old way.
    """
    positions: list[int] = []
    depth = 0
    for offset in range(start, end):
        ch = masked[offset]
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            positions.append(offset)
    return positions


def _normalize_arrow_switches(source: str) -> str:
    """Best-effort rewrite of Java 14+ arrow-style `switch` case labels
    (`case X -> stmt;`, `default -> stmt;`) into `javalang`-compatible
    colon-style (`case X: stmt; break;`), so files using this newer syntax
    can still be parsed structurally.

    Only ever inserts same-line text (`: ` in place of `->`, `break; `
    appended at each arm's end) -- never a newline -- so every line number
    in the result matches the original exactly, which is what lets the
    rest of this module keep treating line numbers as ground truth.

    Deliberately does not attempt to detect whether a given `switch` is a
    *statement* (what this rewrite actually fixes) or an *expression*
    (`var x = switch (y) { ... }`, a different grammar addition entirely
    that recoloring case labels can't fix). That's safe by construction:
    `javalang` has no notion of switch-as-expression regardless of label
    style, so an expression-form switch simply fails to parse again after
    this rewrite too -- same outcome as today, never a silently wrong tree.
    """
    masked = strip_comments_and_literals(source)
    switch_open_braces = _find_switch_body_open_braces(masked)
    if not switch_open_braces:
        return source

    n = len(masked)
    edits: list[tuple[int, int, str]] = []
    brace_depth = 0
    switch_body_depths: list[int] = []
    pending_break: list[bool] = []
    i = 0
    while i < n:
        ch = masked[i]
        if ch == "{":
            brace_depth += 1
            if i in switch_open_braces:
                switch_body_depths.append(brace_depth)
                pending_break.append(False)
            i += 1
            continue
        if ch == "}":
            if switch_body_depths and switch_body_depths[-1] == brace_depth:
                if pending_break[-1]:
                    edits.append((i, i, "break; "))
                switch_body_depths.pop()
                pending_break.pop()
            brace_depth -= 1
            i += 1
            continue

        if switch_body_depths and brace_depth == switch_body_depths[-1]:
            is_case = masked[i : i + 4] == "case" and (i == 0 or not masked[i - 1].isalnum())
            is_default = masked[i : i + 7] == "default" and (i == 0 or not masked[i - 1].isalnum())
            label_end = i + 4 if is_case else i + 7 if is_default else None
            if label_end is not None and not masked[label_end : label_end + 1].isalnum():
                arrow_pos = _find_label_terminator(masked, label_end)
                if arrow_pos is not None:
                    if pending_break[-1]:
                        edits.append((i, i, "break; "))
                    if is_case:
                        for comma_pos in _find_top_level_commas(masked, label_end, arrow_pos):
                            edits.append((comma_pos, comma_pos + 1, ": case"))
                    edits.append((arrow_pos, arrow_pos + 2, ": "))
                    pending_break[-1] = True
                    i = arrow_pos + 2
                    continue
        i += 1

    if not edits:
        return source

    edits.sort(key=lambda edit: edit[0])
    result: list[str] = []
    cursor = 0
    for start, end, replacement in edits:
        result.append(source[cursor:start])
        result.append(replacement)
        cursor = end
    result.append(source[cursor:])
    return "".join(result)


def parse_java_file(path: Path, repo_root: Path) -> list[ParsedClass]:
    """Parse one `.java` file into zero or more `ParsedClass` records
    (zero for files with no type declarations; more than one for files
    with nested static classes, as seen in this codebase's DTOs).

    Raises:
        JavaParseError: if `javalang` cannot parse the file at all. Callers
            (see `parse_source_tree`) are expected to catch this per-file
            and continue, not abort the whole run.
    """
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = javalang.parse.parse(source)
    except javalang.parser.JavaSyntaxError as exc:
        # `javalang` doesn't understand Java 14+ arrow-style `case X -> ...`
        # switch labels at all. Retry once against a normalized copy before
        # giving up -- see _normalize_arrow_switches for why this can only
        # help, never silently produce a wrong parse.
        recovered_tree = None
        if "->" in source:
            normalized = _normalize_arrow_switches(source)
            if normalized != source:
                try:
                    recovered_tree = javalang.parse.parse(normalized)
                    source = normalized
                except (javalang.parser.JavaSyntaxError, javalang.tokenizer.LexerError):
                    recovered_tree = None

        if recovered_tree is None:
            # JavaSyntaxError.__init__ calls super().__init__() with no
            # arguments, so str(exc) is always empty -- the actual detail
            # lives in .description ("Expected ':'") and .at (the offending
            # token and its position), which is what we want surfaced instead.
            detail = exc.description
            if exc.at is not None:
                detail = f"{detail} (at {exc.at})"
            raise JavaParseError(f"{path}: {detail}") from exc

        logger.info(
            "%s: recovered using arrow-switch (`case X -> ...`) compatibility normalization", path
        )
        tree = recovered_tree
    except javalang.tokenizer.LexerError as exc:
        raise JavaParseError(f"{path}: {exc}") from exc

    source_lines = source.splitlines()
    package = tree.package.name if tree.package else ""
    relative_path = str(path.relative_to(repo_root)).replace("\\", "/")
    imports = [imp.path for imp in (tree.imports or [])]
    classes: list[ParsedClass] = []

    # javalang's Node.filter() matches by exact type, not isinstance() --
    # passing it a tuple/list of types (as one might expect) silently
    # matches nothing. Filter for each declaration kind separately and
    # merge, rather than relying on a single combined call.
    type_nodes = [node for decl_type in _TYPE_DECLARATIONS for _, node in tree.filter(decl_type)]
    for type_node in type_nodes:
        class_annotations = [_parse_annotation(a) for a in getattr(type_node, "annotations", [])]
        class_type = _infer_class_type(package, relative_path, class_annotations)
        decl_index = _find_type_declaration_index(source, type_node.name)

        methods: list[ParsedMethod] = []
        method_nodes: list[Any] = list(getattr(type_node, "constructors", []) or [])
        method_nodes += list(getattr(type_node, "methods", []) or [])
        for method_node in method_nodes:
            methods.append(_parse_method(method_node, source_lines, source))

        # Used by security_scanner.py to bound a whole-class text scan
        # (fields included, not just method bodies) -- the same
        # brace-counting heuristic _parse_method uses for a method body.
        class_start_line = type_node.position.line if type_node.position else 1
        class_end_line = _compute_block_end_line(source_lines, class_start_line)

        all_method_annotations = [ann for m in methods for ann in m.annotations]
        classes.append(
            ParsedClass(
                file_path=relative_path,
                package=package,
                class_name=type_node.name,
                class_type=class_type,
                javadoc=_extract_javadoc_before(source, max(decl_index, 0)),
                annotations=class_annotations,
                methods=methods,
                rest_endpoints=_extract_rest_endpoints(class_annotations, all_method_annotations),
                imports=imports,
                start_line=class_start_line,
                end_line=class_end_line,
            )
        )

    return classes


def parse_source_tree(
    repo_root: Path, *, include_tests: bool = False, max_files: int | None = None
) -> ParseResult:
    """Parse every discovered `.java` file under `repo_root` into a single
    `ParseResult`. A file that fails to parse is recorded in
    `parse_errors` and skipped — it never aborts the run.
    """
    files = discover_java_files(repo_root, include_tests=include_tests, max_files=max_files)
    logger.info("Discovered %d Java file(s) to parse", len(files))

    classes: list[ParsedClass] = []
    parse_errors: list[str] = []

    for file_path in files:
        try:
            classes.extend(parse_java_file(file_path, repo_root))
        except JavaParseError as exc:
            logger.warning("Skipping unparseable file: %s", exc)
            parse_errors.append(str(exc))

    logger.info(
        "Parsed %d class(es) from %d file(s); %d parse error(s)",
        len(classes),
        len(files),
        len(parse_errors),
    )
    return ParseResult(classes=classes, parse_errors=parse_errors, files_discovered=len(files))
