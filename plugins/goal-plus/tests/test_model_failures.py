from goal_plus.execution.model_failures import normalize_model_failure


def test_normalize_model_failure_classifies_provider_details_without_secret_values():
    failure = normalize_model_failure(
        "HTTP 401: Bearer sk-super-secret-token-value",
        phase="response",
        provider="direct",
        model="gpt-5.6-luna",
        source="pi_rpc",
    )

    assert failure is not None
    assert failure.code == "credential_failure"
    assert failure.upstream_status == 401
    assert failure.provider == "direct"
    assert failure.model == "gpt-5.6-luna"
    assert "super-secret" not in failure.message
    assert "<redacted>" in failure.message


def test_normalize_model_failure_classifies_retryability_and_model_errors():
    rate_limit = normalize_model_failure("429 rate limit exceeded", model="m")
    missing = normalize_model_failure("404 model not found", model="m")

    assert rate_limit is not None and rate_limit.code == "rate_limited" and rate_limit.retryable
    assert missing is not None and missing.code == "model_not_found" and not missing.retryable
