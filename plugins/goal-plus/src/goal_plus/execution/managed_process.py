"""Wait and stop an exact local process already launched by its host owner."""

import subprocess
import time
from collections.abc import Callable
from typing import Literal

from goal_plus.execution.process_identity import inspect_process
from goal_plus.execution.process_monitor import ProcessMonitor
from goal_plus.execution.process_output import ProcessOutput, ProcessOutputLimitExceeded
from goal_plus.execution.process_resources import OwnedProcess, stop_processes
from goal_plus.ports.process import ProcessOutcome
from goal_plus.ports.process_identity import ProcessWitness

# Existing inference polling, TERM grace, and final pipe-drain intervals.
PROCESS_POLL_SECONDS = 0.5
PROCESS_TERM_SECONDS = 1.0
PROCESS_KILL_SECONDS = 1.0
PROCESS_DRAIN_SECONDS = 1.0


def _complete_output(current: str, observed: str | bytes | None) -> str:
    if observed is None:
        return current
    candidate = observed.decode("utf-8", errors="replace") if isinstance(observed, bytes) else observed
    return candidate if len(candidate.encode("utf-8")) >= len(current.encode("utf-8")) else current


class ManagedProcess:
    def __init__(
        self, process: subprocess.Popen[str], *, output_limit_bytes: int | None = None,
        term_timeout: float = PROCESS_TERM_SECONDS, kill_timeout: float = PROCESS_KILL_SECONDS,
    ) -> None:
        self.process = process
        self.output_limit_bytes = output_limit_bytes
        self.term_timeout = term_timeout
        self.kill_timeout = kill_timeout
        self._process_group: OwnedProcess | None = None

    def own_process_group(
        self, *, remember: Callable[[ProcessWitness], None] | None = None,
        recall: Callable[[], ProcessWitness | None] | None = None,
    ) -> None:
        """Bind a host-launched dedicated group before admitting work to it."""
        identity = inspect_process(self.process.pid)
        resource = OwnedProcess(
            self.process.pid, identity.start_token if identity else None, self.process.pid,
            remember=remember, recall=recall,
        )
        resource.running()
        self._process_group = resource

    def terminate(self) -> None:
        process = self.process
        if self._process_group is not None:
            outcome, = stop_processes(
                [self._process_group], term_timeout=self.term_timeout, kill_timeout=self.kill_timeout,
            )
            if not outcome.stopped:
                detail = "; ".join(outcome.errors) or "process group still running"
                raise RuntimeError(f"process_group_cleanup_failed: {process.pid}: {detail}")
            process.wait(timeout=self.kill_timeout)
            return
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=self.term_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self.kill_timeout)

    def close(self) -> None:
        """Abandon output only after the owned child has been reaped."""
        self.terminate()
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(self.process, name, None)
            if stream is not None:
                stream.close()

    def _collect_after_termination(self, output: ProcessOutput, stdout: str, stderr: str) -> tuple[str, str]:
        try:
            final_stdout, final_stderr = output.communicate(
                input=None, timeout=PROCESS_DRAIN_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            final_stdout, final_stderr = exc.output, exc.stderr
            return _complete_output(stdout, final_stdout), _complete_output(stderr, final_stderr)
        return final_stdout, final_stderr

    def wait(
        self,
        timeout: float,
        *,
        monitor: ProcessMonitor,
        is_active: Callable[[], bool] | None,
        timeout_detail: str,
        cancellation_detail: str,
        active_check: Literal["before_wait", "after_timeout"] = "before_wait",
    ) -> ProcessOutcome:
        started = time.monotonic()
        stdout = ""
        stderr = ""
        output = None

        def stop(state: Literal["timed_out", "terminated"]) -> ProcessOutcome:
            self.terminate()
            final_stdout, final_stderr = self._collect_after_termination(output, stdout, stderr)
            monitor.observe(
                state, self.process, stdout=final_stdout, stderr=final_stderr,
                detail=timeout_detail if state == "timed_out" else cancellation_detail, force=True,
            )
            return ProcessOutcome(state, final_stdout, final_stderr)

        try:
            output = ProcessOutput(
                self.process, limit_bytes=self.output_limit_bytes,
                before_reap=self._process_group.running if self._process_group is not None else None,
            )
            monitor.observe("running", self.process, force=True)
            while True:
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    return stop("timed_out")
                if active_check == "before_wait" and is_active is not None and not is_active():
                    return stop("terminated")
                try:
                    observed_stdout, observed_stderr = output.communicate(
                        input=None, timeout=min(PROCESS_POLL_SECONDS, remaining),
                    )
                except subprocess.TimeoutExpired as exc:
                    stdout = _complete_output(stdout, exc.output)
                    stderr = _complete_output(stderr, exc.stderr)
                    monitor.observe("running", self.process, stdout=stdout, stderr=stderr)
                    if active_check == "after_timeout" and is_active is not None and not is_active():
                        return stop("terminated")
                    continue
                stdout, stderr = observed_stdout, observed_stderr
                monitor.observe(
                    "process_exited", self.process, stdout=stdout, stderr=stderr, force=True,
                )
                return ProcessOutcome("process_exited", stdout, stderr)
        except ProcessOutputLimitExceeded as exc:
            self.close()
            monitor.observe(
                "failed", self.process, stdout=exc.stdout, stderr=exc.stderr, detail=str(exc), force=True,
            )
            return ProcessOutcome("output_limit_exceeded", exc.stdout, exc.stderr, str(exc))
        except BaseException:
            self.close()
            raise
        finally:
            if output is not None:
                output.close()
