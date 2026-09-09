#!/usr/bin/env python
"""CLI entrypoint for intellisource-ai.

Thin by design: parses arguments, resolves settings, configures logging,
and hands off to `pipeline.run_pipeline`. No business logic lives here --
see `src/intellisource_ai/` for that.

Usage:
    python main.py --repo-url https://github.com/<owner>/<repo>

Run `python main.py --help` for the full flag list.
"""

from __future__ import annotations

import sys

from intellisource_ai.config import build_arg_parser, resolve_settings
from intellisource_ai.exceptions import IntelliSourceAIError
from intellisource_ai.logging_config import configure_logging
from intellisource_ai.pipeline import run_pipeline


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    configure_logging(verbose=args.verbose, quiet=args.quiet)

    try:
        settings = resolve_settings(args)
        #Call the pipeline to run the analysis
        run_pipeline(settings)
    except IntelliSourceAIError as exc:
        # Every failure mode this tool anticipates is one of our own named
        # exceptions (see exceptions.py) -- print a clean message instead
        # of a raw traceback, and exit non-zero for scripting/CI use.
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
