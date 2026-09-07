"""Single-flight detached execution without owning task claim or retry policy."""

import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path

from goal_plus.ports.clock import Clock
from goal_plus.ports.drainer import DrainerRecord, DrainerState, DrainerStopResult
from goal_plus.ports.process_identity import ProcessSnapshot, ProcessWitness
from goal_plus.execution import process_identity as inspection
from goal_plus.execution.process_identity import ProcessInspectionUnavailable
from goal_plus.execution.process_resources import OwnedProcess, stop_processes
from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.execution.launch_gate import ProcessLaunchGate

# Preserve the existing detached Annotation/Scheduler stop bounds.
KILL_GRACE_SECONDS = 1.0


class DetachedDrainer:
    def __init__(
        self, *, state: DrainerState, root_dir: Path, run_id: str, module: str,
        source_root: Path, label: str, clock: Clock,
        worker_identity: Callable[[int, str, str], ProcessSnapshot | None],
        popen: Callable[..., subprocess.Popen[str]] | None = None,
    ) -> None:
        self.state = state
        self.root_dir = root_dir
        self.run_id = run_id
        self.module = module
        self.source_root = source_root
        self.label = label
        self.clock = clock
        self.worker_identity = worker_identity
        self.popen = popen or subprocess.Popen

    def _owned(self, generation: str) -> bool:
        worker = self.state.read_worker()
        return worker is not None and worker.generation == generation and worker.pid == os.getpid()

    def _resource(self, worker: DrainerRecord) -> OwnedProcess:
        identity = self.worker_identity(worker.pid, self.run_id, worker.generation)
        if worker.witness is not None:
            token = worker.witness.members.get(worker.pid)
            if token is None:
                raise ProcessInspectionUnavailable("drainer witness is missing its launch identity")
        else:
            token = identity.start_token if identity else None
        return OwnedProcess(worker.pid, token, worker.pid, witness=worker.witness)

    def _remember(self, worker: DrainerRecord, witness: ProcessWitness) -> None:
        with self.state.worker_lock():
            current = self.state.read_worker()
            if current is None or (current.generation, current.pid) != (worker.generation, worker.pid):
                return
            self._remember_locked(current, witness)

    def _remember_locked(self, worker: DrainerRecord, witness: ProcessWitness) -> None:
        previous = worker.witness
        if previous is not None:
            if previous.boot_id != witness.boot_id:
                raise ProcessInspectionUnavailable("drainer witness belongs to another boot")
            witness = witness.model_copy(update={"members": {**previous.members, **witness.members}})
        if witness != previous:
            self.state.write_worker(worker.model_copy(update={"witness": witness}))

    def start(
        self, *, has_work: Callable[[], bool], environment: Callable[[], Mapping[str, str]],
        register_process: Callable[[int], str] | None = None,
    ) -> bool:
        """Caller holds scope admission; worker lock serializes registration and exit."""
        with self.state.worker_lock():
            if not has_work():
                return False
            current = self.state.read_worker()
            if current is not None:
                resource = self._resource(current)
                resource.remember = lambda witness: self._remember_locked(current, witness)
                if resource.running():
                    return False
            generation = uuid.uuid4().hex
            env = dict(environment())
            pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = str(self.source_root) + (os.pathsep + pythonpath if pythonpath else "")
            command = [
                sys.executable, "-m", self.module, "drain", "--root", str(self.root_dir),
                "--run-id", self.run_id, "--generation", generation,
            ]
            with ProcessLaunchGate() as gate, ExitStack() as startup:
                with self.state.open_log() as log:
                    process = self.popen(
                        gate.command(command), pass_fds=gate.pass_fds,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        close_fds=True, env=env, start_new_session=True,
                    )
                    # claim() cannot pass this worker lock until registration succeeds.
                    startup.callback(ManagedProcess(process).close)
                identity = inspection.inspect_process(int(process.pid))
                resource = OwnedProcess(int(process.pid), identity.start_token if identity else None, int(process.pid))
                if not resource.running():
                    raise RuntimeError("drainer process exited before registration")
                self.state.write_worker(DrainerRecord(
                    generation=generation, pid=int(process.pid), started_at=self.clock.timestamp(),
                    witness=resource.witness,
                ))
                if register_process is not None:
                    register_process(int(process.pid))
                gate.release()
                startup.pop_all()
            return True

    @contextmanager
    def claim(self, generation: str | None) -> Iterator[bool]:
        with self.state.drain_lock():
            owned = True
            if generation is not None:
                with self.state.worker_lock():
                    owned = self._owned(generation)
            yield owned

    def finish_if_idle(self, generation: str | None, *, has_work: Callable[[], bool]) -> bool:
        if generation is None:
            return True
        # A concurrent kick either leaves work for this generation or starts the next.
        with self.state.worker_lock():
            if not self._owned(generation):
                return True
            if has_work():
                return False
            self.state.remove_worker()
            return True

    def close(self, *, timeout_seconds: float = 5.0) -> DrainerStopResult | None:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be >= 0")
        with self.state.worker_lock():
            worker = self.state.read_worker()
            if worker is None:
                return None
            resource = self._resource(worker)
            if not resource.running():
                self.state.remove_worker()
                return DrainerStopResult(**worker.model_dump(exclude={"witness"}), status="stale")

        resource.remember = lambda witness: self._remember(worker, witness)
        if resource.witness is not None:
            self._remember(worker, resource.witness)
        outcome, = stop_processes([resource], term_timeout=timeout_seconds, kill_timeout=KILL_GRACE_SECONDS)
        if not outcome.stopped:
            raise RuntimeError(f"{self.label} {worker.pid} did not stop: {list(outcome.errors)}")
        with self.state.worker_lock():
            latest = self.state.read_worker()
            if latest is not None and latest.generation == worker.generation:
                self.state.remove_worker()
        return DrainerStopResult(
            **worker.model_dump(exclude={"witness"}), status="stopped", finished_at=self.clock.timestamp(),
        )
