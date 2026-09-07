"""Read-only host process inspection; unavailable is neither absent nor owned."""

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from goal_plus.ports.process_identity import ProcessIdentity, ProcessSnapshot

EXITED_PROCESS_STATES = frozenset({"Z", "X", "x"})
RECOVERY_INSPECTION_TIMEOUT_SECONDS = 3


class ProcessInspectionUnavailable(RuntimeError):
    pass


def boot_identity() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        if not value:
            raise ValueError("empty boot identity")
        return value
    except (OSError, ValueError) as exc:
        raise ProcessInspectionUnavailable("ProcessInspectionUnavailable: boot_identity") from exc


def inspect_process(pid: int) -> ProcessSnapshot | None:
    if pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError) as exc:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            pass
        raise ProcessInspectionUnavailable(f"ProcessInspectionUnavailable: {pid}:stat") from exc
    except (OSError, UnicodeError) as exc:
        raise ProcessInspectionUnavailable(f"ProcessInspectionUnavailable: {pid}:stat") from exc
    try:
        # proc_pid_stat(5): comm may contain spaces and ')'; fields 3, 5, and 22 follow it.
        fields = raw.rsplit(")", 1)[1].split()
        observed_pid = int(raw.split("(", 1)[0].strip())
        group = int(fields[2])
        if observed_pid != pid or group < 0 or len(fields[0]) != 1 or not fields[19].isdecimal():
            raise ValueError("invalid process stat")
        return ProcessSnapshot(pid, fields[19], group, fields[0])
    except (IndexError, ValueError) as exc:
        raise ProcessInspectionUnavailable(f"ProcessInspectionUnavailable: {pid}:stat") from exc


def process_alive(pid: int, start_token: str | None = None) -> bool:
    observed = inspect_process(pid)
    return observed is not None and observed.state not in EXITED_PROCESS_STATES and (
        start_token is None or observed.start_token == start_token
    )


def inspect_worker(
    pid: int, run_id: str, generation: str, *, module: str, root_dir: Path,
) -> ProcessSnapshot | None:
    if pid <= 0 or not generation:
        return None
    return inspect_command(pid, [
        "-m", module, "drain", "--root", str(root_dir),
        "--run-id", run_id, "--generation", generation,
    ])


def inspect_command(pid: int, arguments: Sequence[str]) -> ProcessSnapshot | None:
    """Match the trusted launch argv after the executable, retaining its identity."""
    if pid <= 0:
        return None
    before = inspect_process(pid)
    if before is None or before.state in EXITED_PROCESS_STATES:
        return None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError as exc:
        if not process_alive(pid, before.start_token):
            return None
        raise ProcessInspectionUnavailable(f"ProcessInspectionUnavailable: {pid}:cmdline") from exc
    after = inspect_process(pid)
    if after is None or after.start_token != before.start_token or after.state in EXITED_PROCESS_STATES:
        return None
    if not raw or not raw.endswith(b"\0"):
        raise ProcessInspectionUnavailable(f"ProcessInspectionUnavailable: {pid}:cmdline")
    command = [os.fsdecode(arg) for arg in raw[:-1].split(b"\0")]
    return after if command[1:] == list(arguments) else None


def inspect_process_group(group_id: int) -> tuple[ProcessSnapshot, ...]:
    if group_id <= 1:
        raise ProcessInspectionUnavailable(f"ProcessInspectionUnavailable: {group_id}:invalid_group")
    members = []
    try:
        for path in Path("/proc").iterdir():
            if not path.name.isdecimal():
                continue
            pid = int(path.name)
            try:
                if os.getpgid(pid) != group_id:
                    continue
            except ProcessLookupError:
                continue
            observed = inspect_process(pid)
            if observed is not None and observed.process_group_id == group_id:
                members.append(observed)
    except OSError as exc:
        raise ProcessInspectionUnavailable(f"ProcessInspectionUnavailable: {group_id}:group_members") from exc
    return tuple(members)


def process_identity(pid: int) -> ProcessIdentity | None:
    """Read the existing recovery format explicitly, without converting procfs ticks."""
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat=", "-o", "lstart="],
        capture_output=True, text=True, check=False, timeout=RECOVERY_INSPECTION_TIMEOUT_SECONDS,
    )
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return None
    if result.returncode or not result.stdout.strip():
        raise ProcessInspectionUnavailable("RecoveryProcessInspectionUnavailable")
    parts = result.stdout.strip().split(maxsplit=1)
    if parts[0].startswith("Z"):
        return None
    if len(parts) != 2:
        raise ProcessInspectionUnavailable("RecoveryProcessInspectionUnavailable")
    return ProcessIdentity(pid=pid, started=parts[1].strip())
