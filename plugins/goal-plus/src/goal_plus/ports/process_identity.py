"""Host observations; the existing scope and recovery tokens are not interchangeable."""

from dataclasses import dataclass
from typing import Annotated
from pydantic import Field

from goal_plus.domain.base import SearchModel


class ProcessIdentity(SearchModel):
    """The existing recovery record's ps lstart identity."""

    pid: int
    started: str


class ProcessWitness(SearchModel):
    """Verified members of one registered resource, never a PID-only ownership grant."""

    boot_id: str = Field(min_length=1)
    members: dict[Annotated[int, Field(gt=0)], Annotated[str, Field(min_length=1)]]


@dataclass(frozen=True)
class ProcessSnapshot:
    """Linux procfs fields, including the scope record's starttime token."""

    pid: int
    start_token: str
    process_group_id: int
    state: str


@dataclass(frozen=True)
class ProcessStopOutcome:
    stopped: bool
    errors: tuple[str, ...] = ()
