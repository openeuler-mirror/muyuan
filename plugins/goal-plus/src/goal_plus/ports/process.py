"""Local process observations, not task completion or host session state."""

from dataclasses import dataclass
from collections.abc import Callable
from typing import Literal


@dataclass(frozen=True)
class ProcessRegistration:
    register: Callable[[int], str]
    finish: Callable[[str], None]


@dataclass(frozen=True)
class ProcessOutcome:
    state: Literal["process_exited", "timed_out", "terminated", "output_limit_exceeded"]
    stdout: str
    stderr: str
    detail: str | None = None
