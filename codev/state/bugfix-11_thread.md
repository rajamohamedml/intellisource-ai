# bugfix-11 thread

- Investigate: confirmed `java_parser.py` imported private `complexity._strip_comments_and_literals` (stage 1 -> stage 2 backwards edge). Root cause is placement of the helper, not behavior.
- Architect direction: pure move into neutral `java_lexing.py`; sync with master first. Merged origin/master (660766b) cleanly; java_parser.py/complexity.py untouched upstream.
- Fix: new `java_lexing.strip_comments_and_literals` (public name, body byte-identical); both modules import it. Added `tests/test_java_lexing.py` (direct tests + AST check that parser/complexity don't import each other). CLAUDE.md pipeline listing notes the new leaf module.
- Gotcha: the shared `.venv` editable install points at the main checkout's `src`, so pytest/porch checks in this worktree need `PYTHONPATH=$PWD/src` or they miss the new module.
- Full suite: 71 passed; ruff + mypy clean.
