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
from goal_plus.execution.managed_process import ManagedProcess
from goal_plus.execution.launch_gate import ProcessLaunchGate
from goal_plus.execution.python_command import module_arguments

class DetachedDrainer:
    def __init__(
        self, *, state: DrainerState, root_dir: Path, run_id: str, module: str,
        source_root: Path, label: str, clock: Clock,
        popen: Callable[..., subprocess.Popen[str]] | None = None,
    ) -> None:
        self.state = state
        self.root_dir = root_dir
        self.run_id = run_id
        self.module = module
        self.source_root = source_root
        self.label = label
        self.clock = clock
        self.popen = popen or subprocess.Popen

    def is_current(self, generation: str | None) -> bool:
        if generation is None:
            return True
        with self.state.worker_lock():
            return self._owned(generation)

    def _owned(self, generation: str) -> bool:
        worker = self.state.read_worker()
        return worker is not None and worker.generation == generation

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
                return False
            generation = uuid.uuid4().hex
            env = dict(environment())
            pythonpath = env.get("PYTHONPATH")
            env["PYTHONPATH"] = str(self.source_root) + (os.pathsep + pythonpath if pythonpath else "")
            command = [
                sys.executable, *module_arguments(self.module, ["drain", "--root", str(self.root_dir),
                "--run-id", self.run_id, "--generation", generation]),
            ]
            with ProcessLaunchGate() as gate, ExitStack() as startup:
                with self.state.open_log() as log:
                    process = self.popen(
                        gate.command(command), pass_fds=gate.pass_fds,
                        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        close_fds=True, env=env,
                    )
                    # claim() cannot pass this worker lock until registration succeeds.
                    startup.callback(ManagedProcess(process).close)
                self.state.write_worker(DrainerRecord(
                    generation=generation, pid=int(process.pid), started_at=self.clock.timestamp(),
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
        """Revoke the generation without inspecting or stopping an OS process."""
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be >= 0")
        with self.state.worker_lock():
            worker = self.state.read_worker()
            if worker is None:
                return None
            self.state.remove_worker()
        return DrainerStopResult(
            **worker.model_dump(), status="released", finished_at=self.clock.timestamp(),
        )
