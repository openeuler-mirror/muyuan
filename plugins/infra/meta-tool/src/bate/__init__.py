"""Contracts, state graphs and validated runtime for BATE meta-tools.

Extraction and host integration are separate layers. The namespace identifies
Branch-Aware Template Extraction; it does not select the predecessor algorithm.
"""

from .graph import StateGraphBuilder
from .models import JsonValue, SemanticType, ToolCall, Trace, stable_json
from .normalization import (
    NormalizationConfig,
    RegexSubstitution,
    ToolRule,
    TraceNormalizer,
)
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
    "NormalizationConfig",
    "RegexSubstitution",
    "SemanticType",
    "StateGraphBuilder",
    "StepExecution",
    "StepOutputPathError",
    "StepSpec",
    "ToolCall",
    "ToolRegistry",
    "ToolRule",
    "Trace",
    "TraceNormalizer",
    "input_binding",
    "stable_json",
    "step_binding",
]

__version__ = "1.0.0"
