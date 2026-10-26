"""What the commands print of a decision: always a role's view, checked before it is shown.

The display itself is `ubo_sentinel.present`, which the review UI shares.
"""

import sys

from ubo_sentinel.present import (  # noqa: F401  (the commands import these from here)
    RenderWithheld,
    audit_head,
    decision_text,
    summary_lines,
)


def utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        # A non-Latin name must not fail a piped or redirected run on Windows.
        sys.stdout.reconfigure(encoding="utf-8")
