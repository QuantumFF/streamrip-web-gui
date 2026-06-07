"""The real `rip` runner forces a wide console so streamrip's RichHandler does
not wrap the skip log line.

The fake-runner tests feed clean single-line strings, so they never exercise
the wrapping that broke skip detection in production: at the piped (non-tty)
default of 80 columns, "...Marked as downloaded in the database." wraps
mid-phrase (with rich's right-aligned source location dropped into the gap),
so SKIP_LINE_RE misses and an already-downloaded album is misclassified as a
fresh completion. These tests drive the actual subprocess runner through a
real RichHandler to lock that fix in.
"""
import sys
import textwrap

import pytest

import app as app_module
from app import classify_download

# A child that logs exactly as streamrip does: basicConfig at INFO with a
# RichHandler, then the per-track "skip" line. RichHandler sizes itself from the
# COLUMNS the runner injects, so this wraps iff that width is narrow.
_RICH_SKIP_PROGRAM = textwrap.dedent(
    """
    import logging
    from rich.logging import RichHandler
    logging.basicConfig(level="INFO", format="%(message)s", datefmt="[%X]",
                        handlers=[RichHandler()])
    log = logging.getLogger("streamrip")
    log.setLevel(logging.INFO)
    log.info("Skipping track 1234567890. Marked as downloaded in the database.")
    """
)


def test_runner_env_forces_wide_columns():
    env = app_module._rip_runner_env()
    assert int(env["COLUMNS"]) >= 200
    # Base environment is preserved (PATH must survive so `rip` is found).
    assert "PATH" in env


def test_skip_line_survives_rich_handler_unwrapped():
    pytest.importorskip("rich")
    lines = list(app_module._default_runner([sys.executable, "-c", _RICH_SKIP_PROGRAM]))
    output = "\n".join(lines)
    # The skip phrase stays contiguous on one line, so detection classifies the
    # run as skipped rather than completed.
    assert app_module.SKIP_LINE_RE.search(output), output
    assert classify_download(0, output) == "skipped"
