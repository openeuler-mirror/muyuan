"""Compatibility exports for the typed ThinkThread Agent POSIX client."""

from goal_plus.thinkthread_agent_posix import (
    AGENT_POSIX_CONTRACT_FINGERPRINT,
    AGENT_POSIX_CONTROL_PROTOCOL_VERSION,
    AgentPosixBridgeError,
    AgentPosixSdkClient,
    AgentPosixSdkMismatch,
    new_request_id,
)

ThinkThreadAgentRuntime = AgentPosixSdkClient
ThinkThreadControlError = AgentPosixBridgeError
ThinkThreadRuntimeUnavailable = AgentPosixBridgeError

__all__ = [
    "AGENT_POSIX_CONTRACT_FINGERPRINT",
    "AGENT_POSIX_CONTROL_PROTOCOL_VERSION",
    "AgentPosixBridgeError",
    "AgentPosixSdkClient",
    "AgentPosixSdkMismatch",
    "ThinkThreadAgentRuntime",
    "ThinkThreadControlError",
    "ThinkThreadRuntimeUnavailable",
    "new_request_id",
]
