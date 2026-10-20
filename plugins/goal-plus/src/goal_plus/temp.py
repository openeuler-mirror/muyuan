"""Checkout-local temporary storage used by Goal Plus runtime helpers.

Runtime scratch data used to fall back to the process-wide ``/tmp``.  On
shared hosts that namespace can run out of inodes even while the checkout's
filesystem has space available.  Keep Goal Plus' short-lived scratch files
under ``.tmp`` in the current working directory instead.  A caller may pass a
different base (for example a candidate workspace) when the temporary data
must live beside that workspace.

The directory is intentionally not part of the persisted ``.gp`` state.  It
is ignored by the repository and every caller owns the returned
``TemporaryDirectory`` lifecycle.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


TMP_ROOT_ENV = "GOAL_PLUS_TMPDIR"


def temporary_root(base: Path | str | None = None) -> Path:
    """Return and create the checkout-local temporary root.

    ``GOAL_PLUS_TMPDIR`` is an explicit operational override for hosts where
    the current working directory is read-only.  Without it, ``base/.tmp`` is
    used; when ``base`` is omitted this is exactly ``Path.cwd() / '.tmp'``.
    """

    configured = os.environ.get(TMP_ROOT_ENV)
    root = Path(configured).expanduser() if configured else Path(base or Path.cwd()) / ".tmp"
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def temporary_directory(
    prefix: str = "goal-plus-",
    *,
    base: Path | str | None = None,
    dir: Path | str | None = None,
    **kwargs: Any,
) -> tempfile.TemporaryDirectory[str]:
    """Create a managed temporary directory below :func:`temporary_root`.

    ``dir`` remains available for call sites that already own an invocation
    directory.  Otherwise the directory is created below ``base/.tmp`` (or
    the current working directory's ``.tmp``).
    """

    target = Path(dir).expanduser() if dir is not None else temporary_root(base)
    target.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=str(target), **kwargs)
