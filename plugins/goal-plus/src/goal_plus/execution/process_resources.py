"""Identity-checked, bounded cleanup of already-owned local execution resources."""

import math
import os
import signal
import time
from collections.abc import Callable, Sequence

from goal_plus.execution import process_identity as inspection
from goal_plus.ports.process_identity import ProcessStopOutcome, ProcessWitness

# Existing scope/drainer polling cadence; callers retain their TERM/KILL budgets.
STOP_POLL_SECONDS = 0.05


class OwnedProcess:
    def __init__(
        self, pid: int, start_token: str | None, process_group_id: int | None = None, *,
        witness: ProcessWitness | None = None, remember: Callable[[ProcessWitness], None] | None = None,
        recall: Callable[[], ProcessWitness | None] | None = None,
    ) -> None:
        self.pid = pid
        self.start_token = start_token
        self.process_group_id = process_group_id
        self.witness = witness.model_copy(deep=True) if witness else None
        self.remember = remember
        self.recall = recall
        self._anchors = dict(witness.members) if witness else ({pid: start_token} if start_token else {})
        self._detached: dict[int, str] = {}
        self._group_owned = False

    def _remember(self) -> None:
        witness = ProcessWitness(boot_id=inspection.boot_identity(), members=self._anchors)
        if witness != self.witness:
            if self.remember is not None:
                self.remember(witness)
            self.witness = witness

    def running(self) -> bool:
        if not isinstance(self.pid, int) or isinstance(self.pid, bool) or self.pid <= 0:
            raise inspection.ProcessInspectionUnavailable("ProcessInspectionUnavailable: invalid_pid")
        if self.start_token is not None and (not isinstance(self.start_token, str) or not self.start_token):
            raise inspection.ProcessInspectionUnavailable(f"ProcessInspectionUnavailable: {self.pid}:invalid_start_token")
        group = self.process_group_id
        if group is not None and (not isinstance(group, int) or group != self.pid or group <= 1):
            raise inspection.ProcessInspectionUnavailable(f"ProcessInspectionUnavailable: {self.pid}:invalid_group_owner")
        if self.recall is not None:
            remembered = self.recall()
            if remembered is not None:
                self.witness = remembered
                self._anchors.update(remembered.members)
        if self.witness is not None and self.witness.boot_id != inspection.boot_identity():
            return False
        leader = inspection.inspect_process(self.pid)
        reused = leader is not None and self.start_token and leader.start_token != self.start_token
        if self.process_group_id is None:
            if reused or leader is None or leader.state in inspection.EXITED_PROCESS_STATES:
                return False
            if not self.start_token:
                raise inspection.ProcessInspectionUnavailable(f"ProcessInspectionUnavailable: {self.pid}:missing_start_token")
            self._remember()
            return True

        members = {} if reused else {item.pid: item for item in inspection.inspect_process_group(group)}
        if leader is not None and leader.start_token == self.start_token:
            if leader.process_group_id != group:
                raise inspection.ProcessInspectionUnavailable(f"ProcessInspectionUnavailable: {self.pid}:group_owner_moved")
            members.setdefault(leader.pid, leader)
        group_active = any(item.state not in inspection.EXITED_PROCESS_STATES for item in members.values())
        self._group_owned = any(self._anchors.get(item.pid) == item.start_token for item in members.values())
        if group_active and not self._group_owned:
            raise inspection.ProcessInspectionUnavailable(f"ProcessInspectionUnavailable: {group}:group_ownership_unknown")
        if self._group_owned:
            self._anchors.update((item.pid, item.start_token) for item in members.values())
        # A previously witnessed member remains owned after leaving the dedicated group.
        self._detached = {}
        for pid, token in self._anchors.items():
            if pid in members:
                continue
            observed = inspection.inspect_process(pid)
            if observed is not None and observed.start_token == token and observed.state not in inspection.EXITED_PROCESS_STATES:
                self._detached[pid] = token
        self._remember()
        return group_active or bool(self._detached)

    def send(self, signum: signal.Signals) -> None:
        if self.running():
            if self.process_group_id is None:
                os.kill(self.pid, signum)
            elif self._group_owned:
                os.killpg(self.process_group_id, signum)
            for pid, token in self._detached.items():
                if inspection.process_alive(pid, token):
                    os.kill(pid, signum)


def stop_processes(
    resources: Sequence[OwnedProcess], *, term_timeout: float, kill_timeout: float,
) -> tuple[ProcessStopOutcome, ...]:
    if any(not math.isfinite(timeout) or timeout < 0 for timeout in (term_timeout, kill_timeout)):
        raise ValueError("process stop timeouts must be finite and >= 0")
    signal_errors: dict[int, list[str]] = {}
    observation_errors: dict[int, str] = {}

    def running(index: int) -> bool:
        try:
            active = resources[index].running()
        except inspection.ProcessInspectionUnavailable as exc:
            observation_errors[index] = str(exc)
            return True
        observation_errors.pop(index, None)
        return active

    def send(index: int, signum: signal.Signals) -> None:
        try:
            resources[index].send(signum)
        except ProcessLookupError:
            pass
        except inspection.ProcessInspectionUnavailable as exc:
            observation_errors[index] = str(exc)
        except PermissionError as exc:
            signal_errors.setdefault(index, []).append(f"process:{resources[index].pid}:PermissionError:{exc}")

    def wait(timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and any(running(index) for index in range(len(resources))):
            time.sleep(STOP_POLL_SECONDS)

    for index in range(len(resources)):
        send(index, signal.SIGTERM)
    wait(term_timeout)
    for index in range(len(resources)):
        if running(index):
            send(index, signal.SIGKILL)
    wait(kill_timeout)
    outcomes = []
    for index in range(len(resources)):
        stopped = not running(index)
        errors = [] if stopped else signal_errors.get(index, []).copy()
        if not stopped and index in observation_errors:
            errors.append(observation_errors[index])
        outcomes.append(ProcessStopOutcome(stopped=stopped, errors=tuple(errors)))
    return tuple(outcomes)
