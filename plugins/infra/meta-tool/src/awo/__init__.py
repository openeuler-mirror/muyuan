"""Core data contracts and deterministic runtime for Meta-tool Online.

The discovery and Pi integration layers build on these small, dependency-free
primitives in later commits.
"""

from .models import JsonValue, SemanticType, ToolCall, Trace, stable_json
from .runtime import (
    MetaToolExecutionError,
    MetaToolExecutor,
    MetaToolResult,
    StepExecution,
    StepOutputPathError,
    ToolRegistry,
)
from .spec import MetaToolSpec, StepSpec, input_binding, step_binding

__all__ = [
    "JsonValue",
    "MetaToolExecutionError",
    "MetaToolExecutor",
    "MetaToolResult",
    "MetaToolSpec",
    "SemanticType",
    "StepExecution",
    "StepOutputPathError",
    "StepSpec",
    "ToolCall",
    "ToolRegistry",
    "Trace",
    "input_binding",
    "stable_json",
    "step_binding",
]

__version__ = "1.0.0"
