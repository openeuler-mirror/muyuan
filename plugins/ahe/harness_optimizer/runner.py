"""真实候选校验运行与指标提取。"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .adapter import resolve_adapter
from .schema import HarnessSpec
from .workspace import CandidateWorkspace


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunRecord:
    command: str | tuple[str, ...]
    cwd: str
    returncode: int | None
    stdout: str
    stderr: str
    duration_s: float
    started_at: str
    finished_at: str
    timed_out: bool = False
    metrics: dict[str, Any] = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        command: str | list[str] = list(self.command) if isinstance(self.command, tuple) else self.command
        return {
            "command": command,
            "cwd": self.cwd,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_s": self.duration_s,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "timed_out": self.timed_out,
            "run_id": self.run_id,
            "ok": self.ok,
            "metrics": self.metrics,
        }


RunResult = RunRecord


def _command_repr(command: str | Sequence[str]) -> str | tuple[str, ...]:
    return command if isinstance(command, str) else tuple(str(item) for item in command)


def run_command(
    command: str | Sequence[str],
    cwd: os.PathLike[str] | str,
    *,
    timeout_s: float = 7200.0,
    env: Mapping[str, str] | None = None,
    shell: bool = False,
) -> RunRecord:
    """运行校验命令并保留完整 stdout/stderr 证据。"""
    if timeout_s <= 0:
        raise ValueError("timeout_s must be greater than zero")
    cwd_path = Path(cwd).resolve()
    if not cwd_path.is_dir():
        raise FileNotFoundError(f"run cwd is not a directory: {cwd}")
    if isinstance(command, str):
        if not command.strip():
            raise ValueError("command must be non-empty")
        argv: str | list[str] = command if shell else shlex.split(command)
    else:
        argv = [str(item) for item in command]
        if not argv:
            raise ValueError("command must contain at least one argument")
        if shell:
            argv = shlex.join(argv)
    started_at = _utc_now()
    started = time.monotonic()
    process_env = os.environ.copy()
    if env:
        process_env.update({str(key): str(value) for key, value in env.items()})
    timed_out = False
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd_path),
            env=process_env,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            shell=shell,
            check=False,
        )
        returncode, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = None
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")
        stderr = f"{stderr}\ncommand timed out after {timeout_s}s".strip()
    finished_at = _utc_now()
    return RunRecord(
        command=_command_repr(command),
        cwd=str(cwd_path),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_s=round(time.monotonic() - started, 6),
        started_at=started_at,
        finished_at=finished_at,
        timed_out=timed_out,
    )


def _number(value: Any) -> int | float | Any:
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        try:
            number = float(stripped)
        except ValueError:
            return value
        return int(number) if number.is_integer() else number
    return value


def parse_metrics(stdout: str | RunRecord, *, stderr: str = "") -> dict[str, Any]:
    """解析校验命令输出的最后一个 JSON 对象。

    命令可能在结果前输出日志。解析器也接受常见的
    ``accuracy=... tokens=... time_s=...`` 文本形式作为回退。
    """
    if isinstance(stdout, RunRecord):
        stderr = stdout.stderr if not stderr else stderr
        stdout = stdout.stdout
    candidates = [line.strip() for line in stdout.splitlines() if line.strip()]
    objects: list[Mapping[str, Any]] = []
    for candidate in reversed(candidates):
        try:
            value = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(value, Mapping):
            objects.append(value)
            break
    if not objects:
        try:
            value = json.loads(stdout)
        except (TypeError, ValueError):
            value = None
        if isinstance(value, Mapping):
            objects.append(value)
    if objects:
        raw: Mapping[str, Any] = objects[0]
        nested = raw.get("metrics")
        source: Mapping[str, Any] = nested if isinstance(nested, Mapping) else raw
        result = {str(key): value for key, value in source.items()}
    else:
        result = {}
        pattern = re.compile(r"(?P<key>accuracy|tokens|time_s|time|duration_s)\s*[:=]\s*(?P<value>[0-9]+(?:\.[0-9]+)?)", re.I)
        for match in pattern.finditer(stdout):
            result[match.group("key").lower()] = _number(match.group("value"))
    aliases = {
        "time": "time_s",
        "duration_s": "time_s",
        "elapsed_s": "time_s",
        "token_count": "tokens",
        "total_tokens": "tokens",
    }
    for alias, canonical in aliases.items():
        if canonical not in result and alias in result:
            result[canonical] = result[alias]
    for key, value in list(result.items()):
        if key in {"accuracy", "tokens", "time_s"}:
            result[key] = _number(value)
    if not result:
        result["parse_error"] = "no metrics object found"
        if stderr.strip():
            result["stderr_preview"] = stderr[-500:]
    return result


def run_validation(
    spec: HarnessSpec,
    candidate: CandidateWorkspace,
    *,
    adapter: Any = None,
    shell: bool = False,
) -> RunRecord:
    """解析 adapter、执行其命令并附加解析出的指标。"""
    selected = adapter or resolve_adapter(spec)
    command = selected.build_command(spec, candidate.path)
    record = run_command(
        command,
        candidate.path,
        timeout_s=spec.validation.timeout_s,
        env=spec.validation.env,
        shell=shell,
    )
    record.metrics.update(selected.parse_metrics(record.stdout, record.stderr))
    return record


__all__ = ["RunRecord", "RunResult", "parse_metrics", "run_command", "run_validation"]
