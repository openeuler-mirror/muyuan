"""Model-specific tool-call protocols behind one stable API."""

from .profile import ToolCallProfile, ToolCallProtocolCapabilities
from .registry import (
    ProfileRegistrationError,
    ProfileRegistry,
    ProfileResolution,
    ProfileResolutionError,
    ResolutionSource,
    default_profile_registry,
)

__all__ = [
    "ProfileRegistrationError",
    "ProfileRegistry",
    "ProfileResolution",
    "ProfileResolutionError",
    "ResolutionSource",
    "ToolCallProfile",
    "ToolCallProtocolCapabilities",
    "default_profile_registry",
]
