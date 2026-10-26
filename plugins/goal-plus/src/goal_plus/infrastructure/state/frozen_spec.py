"""Read the existing FrozenSpec formats without mutating their stored identity."""

from pathlib import Path

from goal_plus.domain.search import FrozenSpec
from goal_plus.infrastructure.state.files import load_json


def load_frozen_spec(path: Path) -> FrozenSpec:
    data = load_json(path)
    return FrozenSpec.model_validate(data)
