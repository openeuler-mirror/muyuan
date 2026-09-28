"""Pure process-sandbox launch inputs shared by native hosts."""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


SandboxMode = Literal["read-only", "workspace-write"]
SandboxBackend = Literal["native", "outer"]


@dataclass(frozen=True)
class ProcessSandbox:
    """Filesystem contract for one child process.

    ``native`` means the agent host enforces the contract itself (Codex).
    ``outer`` means Goal Plus wraps the child before exec (Pi).
    """

    mode: SandboxMode
    backend: SandboxBackend
    workspace: Path
    writable_roots: tuple[Path, ...] = ()
    network: Literal["allow", "deny"] = "allow"

    def __post_init__(self) -> None:
        if not self.workspace.is_absolute():
            raise ValueError("sandbox workspace must be an absolute path")
        if any(not path.is_absolute() for path in self.writable_roots):
            raise ValueError("sandbox writable roots must be absolute paths")
