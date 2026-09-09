"""Local process observations, not task completion or host session state."""

from dataclasses import dataclass
from collections.abc import Callable
from typing import Literal
from goal_plus.ports.process_identity import ProcessWitness


@dataclass(frozen=True)
class ProcessRegistration:
    register: Callable[[int], str]
    finish: Callable[[str], None]
    remember: Callable[[str, ProcessWitness], None] | None = None
    recall: Callable[[str], ProcessWitness | None] | None = None


@dataclass(frozen=True)
class ProcessOutcome:
    state: Literal["process_exited", "timed_out", "terminated", "output_limit_exceeded"]
    stdout: str
    stderr: str
    detail: str | None = None
