"""Operator-only commands for explicit Evidence replay experiments."""

from __future__ import annotations

import argparse
import json
import uuid

from goal_plus.infrastructure.replay_experiment import FileReplayExperiments
from goal_plus.paths import DEFAULT_RUNTIME_ROOT


def _target(value: str) -> tuple[str, int]:
    candidate_id, separator, iteration = value.rpartition(":")
    if not separator or not candidate_id or not iteration.isdecimal() or int(iteration) < 1:
        raise argparse.ArgumentTypeError("target must be candidate_id:positive_iteration")
    return candidate_id, int(iteration)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="goal-plus replay")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--root", default=DEFAULT_RUNTIME_ROOT)
    inspect.add_argument("--run-id", required=True)
    inspect.add_argument("--candidate-id", required=True)
    create = commands.add_parser("create")
    create.add_argument("--root", default=DEFAULT_RUNTIME_ROOT)
    create.add_argument("--fork-point", required=True)
    create.add_argument("--target", action="append", required=True, type=_target)
    create.add_argument("--request-id", default=None)
    create.add_argument("--no-run", action="store_true")
    for name in ("run", "status", "report", "cleanup"):
        command = commands.add_parser(name)
        command.add_argument("--root", default=DEFAULT_RUNTIME_ROOT)
        command.add_argument("--experiment-id", required=True)
        if name == "cleanup":
            command.add_argument("--confirmed-stopped", action="store_true")
    release = commands.add_parser("release")
    release.add_argument("--root", default=DEFAULT_RUNTIME_ROOT)
    release.add_argument("--fork-point", required=True)
    args = parser.parse_args(argv)
    service = FileReplayExperiments(args.root)
    if args.command == "inspect":
        result = service.inspect(args.run_id, args.candidate_id)
    elif args.command == "create":
        request_id = args.request_id or f"request_{uuid.uuid4().hex}"
        experiment = service.create(args.fork_point, args.target, request_id=request_id)
        result = experiment if args.no_run else service.run(experiment.experiment_id)
        result = result.model_dump(mode="json")
    elif args.command == "run":
        result = service.run(args.experiment_id).model_dump(mode="json")
    elif args.command == "status":
        result = service.read(args.experiment_id).model_dump(mode="json")
    elif args.command == "cleanup":
        result = service.cleanup(
            args.experiment_id, confirmed_stopped=args.confirmed_stopped,
        ).model_dump(mode="json")
    elif args.command == "release":
        result = service.release(args.fork_point).model_dump(mode="json")
    else:
        result = service.report(args.experiment_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
