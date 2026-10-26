"""Scratch space and lifecycle for real-host system tests.

ST scenarios are expensive: each one drives a real host agent and writes runtime
state, sessions and host logs.  They used to be created under
the host's global tempfile directory with ``tempfile.mkdtemp``.  On shared
machines that scatters a large number of inodes across a different mount and
makes cleanup difficult to inspect.  Scratch now lives under the current
checkout's ignored ``.tmp/st`` tree and is removed after each test unless the
test failed or debugging is enabled.

Two rules replace that:

* scratch runs live under ``$PWD/.tmp/st`` by default, and can be relocated
  with ``GOAL_PLUS_ST_TMP``; and
* a run directory is removed once its test finishes, but is kept when the test
  failed or when ``GOAL_PLUS_TEST_DEBUG=1`` is set, so a failure stays
  inspectable.

Cleanup is shared with ordinary pytest directories in ``tests/_test_tmp.py``
and runs after setup, call, and teardown outcomes are known.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from tests._test_tmp import track_test_directory

REPO_ROOT = Path(__file__).resolve().parents[1]

TMP_ENV = "GOAL_PLUS_ST_TMP"


def st_tmp_root() -> Path:
    """Base directory for ST scratch space.

    Override with ``GOAL_PLUS_ST_TMP`` when the default location is unsuitable
    (for example a machine whose checkout lives on a small partition).
    """
    override = os.environ.get(TMP_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return (Path.cwd() / ".tmp" / "st").resolve()


def new_st_run_dir(request: pytest.FixtureRequest, *, kind: str) -> Path:
    """Create a unique scratch directory for a single ST scenario."""
    base = st_tmp_root() / kind
    base.mkdir(parents=True, exist_ok=True)
    node = request.node.name.replace("::", "_").replace("[", "_").replace("]", "")
    return track_test_directory(
        request, Path(tempfile.mkdtemp(prefix=f"{node}-", dir=base))
    )


def new_st_log_dir(node_name: str) -> Path:
    """Return the (persistent, node-keyed) log directory for an ST scenario.

    Logs are small and are the first place to look after a failure, so they
    outlive the run directory; keying by node id keeps the set bounded.
    """
    node = node_name.replace("::", "_").replace("[", "_").replace("]", "")
    log_dir = st_tmp_root() / "st-logs" / node
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir
