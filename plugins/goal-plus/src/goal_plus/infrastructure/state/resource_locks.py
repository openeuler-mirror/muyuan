"""Host-local verifier/device namespace and shared resource exclusion."""

import os
import tempfile
from pathlib import Path
from contextlib import contextmanager
from goal_plus.domain.serialization import sha256_text
from goal_plus.infrastructure.state.files import exclusive_file_lock, load_json, write_json

VERIFIER_RESOURCE_LOCK_DIR_ENV = "GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR"


def host_resource_lock_root() -> Path:
    get_uid = getattr(os, "getuid", None)
    if callable(get_uid):
        default_namespace = f"uid-{get_uid()}"
    else:  # pragma: no cover - Windows fallback
        user_identity = (
            os.environ.get("USERNAME") or os.environ.get("USER") or str(Path.home())
        )
        default_namespace = f"user-{sha256_text(user_identity)[:12]}"
    return Path(
        os.environ.get(
            VERIFIER_RESOURCE_LOCK_DIR_ENV,
            str(
                Path(tempfile.gettempdir())
                / "goal-plus-verifier-locks"
                / default_namespace
            ),
        )
    ).resolve()


@contextmanager
def verifier_resource_lock(resource: str | None, *, request_id: str | None = None):
    if resource is None:
        yield
        return
    lock_root = host_resource_lock_root()
    lock_name = f"{sha256_text(resource)}.lock"
    with exclusive_file_lock(lock_root / lock_name):
        owner_path = lock_root / f"{sha256_text(resource)}.owner.json"
        if owner_path.exists() and (request_id is None or load_json(owner_path).get("request_id") != request_id):
            raise RuntimeError("VerifierResourceNeedsRecovery: unresolved resource owner")
        if request_id is not None:
            write_json(owner_path, {"request_id": request_id})
        # Remote execution may outlive this controller; an exceptional exit retains ownership.
        yield
        if request_id is not None:
            owner_path.unlink(missing_ok=True)
