"""Host-wide device lease registry with existing ownership and rollback semantics."""

import json
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from goal_plus.infrastructure.state.resource_locks import host_resource_lock_root
from goal_plus.domain.search import FrozenSpec, RunState
from goal_plus.domain.serialization import sha256_text
from goal_plus.infrastructure.clock import utc_timestamp as utc_timestamp
from goal_plus.infrastructure.state.files import (
    exclusive_file_lock,
    load_json,
    write_json,
)
from goal_plus.ports.state import RunRecord

from goal_plus.infrastructure.state.search_run import FileSearchRunState
from goal_plus.ports.search_run import DEVICE_LEASE_RELEASE_STATES


@dataclass(frozen=True)
class FileDeviceLeaseReservation:
    root_dir: Path
    run_id: str
    leases: list[tuple[str, Path]]

    def commit(self) -> None:
        owner = {
            "root_dir": str(self.root_dir),
            "run_id": self.run_id,
            "acquired_at": utc_timestamp(),
        }
        written: list[Path] = []
        try:
            for pool_id, path in self.leases:
                write_json(path, {"pool_id": pool_id, **owner})
                written.append(path)
        except Exception:
            for path in written:
                path.unlink(missing_ok=True)
            raise


class FileDeviceLeases:
    def __init__(self, root_dir: Path | str, state: FileSearchRunState) -> None:
        self.root_dir, self.state = Path(root_dir), state

    @staticmethod
    def device_pool_lease_path(pool_id: str) -> Path:
        lease_dir = host_resource_lock_root() / "device-pool-leases"
        return lease_dir / f"{sha256_text(pool_id)}.json"

    @staticmethod
    def device_pool_lease_owner_is_active(owner: dict[str, Any]) -> bool:
        root_dir = owner.get("root_dir")
        run_id = owner.get("run_id")
        if not isinstance(root_dir, str) or not isinstance(run_id, str):
            return True
        try:
            state = RunState(
                load_json(Path(root_dir) / "runs" / run_id / "run.json").get("state")
            )
        except (
            FileNotFoundError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return True
        return state not in DEVICE_LEASE_RELEASE_STATES

    @contextmanager
    def reservation(
        self,
        run: RunRecord,
        frozen: FrozenSpec,
    ):
        pool_ids = sorted(frozen.device_inventory)
        if not pool_ids:
            yield FileDeviceLeaseReservation(self.root_dir, run.run_id, [])
            return

        lease_dir = host_resource_lock_root() / "device-pool-leases"
        lease_dir.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(lease_dir / "registry.lock"):
            leases_to_write: list[tuple[str, Path]] = []
            for pool_id in pool_ids:
                path = self.device_pool_lease_path(pool_id)
                if not path.exists():
                    leases_to_write.append((pool_id, path))
                    continue
                try:
                    owner = load_json(path)
                except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"invalid device pool lease: {path}") from exc
                if not isinstance(owner, dict):
                    raise RuntimeError(f"invalid device pool lease: {path}")
                same_owner = (
                    owner.get("root_dir") == str(self.root_dir)
                    and owner.get("run_id") == run.run_id
                )
                if same_owner:
                    continue
                if self.device_pool_lease_owner_is_active(owner):
                    raise RuntimeError(
                        f"device pool {pool_id!r} is leased by run "
                        f"{owner.get('run_id')!r} under {owner.get('root_dir')!r}"
                    )
                leases_to_write.append((pool_id, path))

            yield FileDeviceLeaseReservation(self.root_dir, run.run_id, leases_to_write)

    def release_device_pool_leases(self, run: RunRecord) -> None:
        frozen = self.state.read_frozen_spec(run.frozen_spec_id)
        pool_ids = sorted(frozen.device_inventory)
        if not pool_ids:
            return
        lease_dir = host_resource_lock_root() / "device-pool-leases"
        with exclusive_file_lock(lease_dir / "registry.lock"):
            for pool_id in pool_ids:
                path = self.device_pool_lease_path(pool_id)
                try:
                    owner = load_json(path)
                except (FileNotFoundError, OSError, json.JSONDecodeError):
                    continue
                if isinstance(owner, dict) and (
                    owner.get("root_dir") == str(self.root_dir)
                    and owner.get("run_id") == run.run_id
                ):
                    path.unlink(missing_ok=True)
