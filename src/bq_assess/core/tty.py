"""Terminal detection for default-on interactive prompts.

The --interactive flag was removed 2026-08-12 (Ryan's directive): prompting is
the DEFAULT behaviour whenever the process is attached to a terminal, in both
bq-collect and bq-assess. Scripted/CI runs (non-TTY) skip every prompt and take
the documented non-interactive fallbacks instead — no run ever blocks on stdin
it can't read.
"""

from __future__ import annotations

import sys


def prompts_enabled() -> bool:
    """True when both stdin and stdout are attached to a terminal."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        # stdin/stdout replaced or closed (test harnesses, frozen apps)
        return False
