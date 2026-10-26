from __future__ import annotations

from typing import Any, Mapping

from auto_pipeline.converter.registry import register_converter
from auto_pipeline.generator.registry import register_generator

from .converter import GoalPlusToCannConverter
from .generator import create_goal_plus_generator


def _create_converter(config: Mapping[str, Any]) -> GoalPlusToCannConverter:
    return GoalPlusToCannConverter(timeout_sec=int(config.get("timeout_sec", 7200)))


def register_integrations(*, replace: bool = True) -> None:
    """Register Muyuan-owned extensions without modifying the third-party library."""

    register_generator("goal-plus", create_goal_plus_generator, replace=replace)
    register_converter("goal-plus", "cann", _create_converter, replace=replace)
