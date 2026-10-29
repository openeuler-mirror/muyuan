"""Core data contracts and validated deterministic runtime for BATE meta-tools.

Extraction and host integration are separate layers. The legacy package
namespace is retained; it does not select the older extraction algorithm.
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
from .validation import MetaToolValidationError

__all__ = [
    "JsonValue",
    "MetaToolExecutionError",
    "MetaToolExecutor",
    "MetaToolResult",
    "MetaToolSpec",
    "MetaToolValidationError",
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
