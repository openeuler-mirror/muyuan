"""Shared bounded verifier log rendering."""

from goal_plus.infrastructure.process_output import OUTPUT_LIMIT_BYTES as VERIFIER_OUTPUT_LIMIT_BYTES

VERIFIER_LOG_LIMIT_BYTES = VERIFIER_OUTPUT_LIMIT_BYTES * 2 + 8192


def bounded_verifier_log(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= VERIFIER_LOG_LIMIT_BYTES:
        return value
    marker = b"[... log truncated ...]\n"
    tail = encoded[-(VERIFIER_LOG_LIMIT_BYTES - len(marker)) :]
    return (marker + tail).decode("utf-8", errors="replace")
