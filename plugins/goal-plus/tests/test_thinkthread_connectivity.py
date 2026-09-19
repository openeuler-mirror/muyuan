from __future__ import annotations

import pytest

from goal_plus.thinkthread_connectivity import (
    ThinkThreadConnectivityError,
    preflight_thinkthread_codex,
)


def _write_config(tmp_path, body: str) -> None:
    config_dir = tmp_path / "codex"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(body, encoding="utf-8")


def test_preflight_requires_isolated_absolute_codex_config(monkeypatch):
    monkeypatch.delenv("CODEX_CONFIG_DIR", raising=False)
    with pytest.raises(ThinkThreadConnectivityError, match="CODEX_CONFIG_DIR"):
        preflight_thinkthread_codex()


def test_preflight_rejects_implicit_openai_codex_fallback(tmp_path, monkeypatch):
    _write_config(tmp_path, 'model = "gpt-5.6-luna"\n')
    monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path / "codex"))
    with pytest.raises(ThinkThreadConnectivityError, match="openai-codex fallback"):
        preflight_thinkthread_codex()


def test_preflight_requires_provider_endpoint_and_credentials(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        'model = "gpt-5.6-luna"\nmodel_provider = "direct"\n'
        "[model_providers.direct]\n",
    )
    monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path / "codex"))
    with pytest.raises(ThinkThreadConnectivityError, match="no base_url"):
        preflight_thinkthread_codex()

    config_path = tmp_path / "codex" / "config.toml"
    config_path.write_text(
        'model = "gpt-5.6-luna"\nmodel_provider = "direct"\n'
        "[model_providers.direct]\n"
        'base_url = "http://127.0.0.1:18081/v1"\n'
        'env_key = "DIRECT_TOKEN"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("DIRECT_TOKEN", raising=False)
    with pytest.raises(ThinkThreadConnectivityError, match="no credential"):
        preflight_thinkthread_codex()


def test_preflight_returns_provider_metadata_without_live_probe(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        'model = "gpt-5.6-luna"\nmodel_provider = "direct"\n'
        'model_reasoning_effort = "high"\n'
        "[model_providers.direct]\n"
        'base_url = "http://127.0.0.1:18081/v1"\n'
        'env_key = "DIRECT_TOKEN"\n',
    )
    monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path / "codex"))
    monkeypatch.setenv("DIRECT_TOKEN", "test-token")
    result = preflight_thinkthread_codex()

    assert result == {
        "model": "gpt-5.6-luna",
        "provider": "direct",
        "endpoint": "http://127.0.0.1:18081/v1",
        "credential": "DIRECT_TOKEN",
    }


def test_preflight_does_not_report_provider_failure_before_execution(tmp_path, monkeypatch):
    _write_config(
        tmp_path,
        'model = "gpt-5.6-luna"\nmodel_provider = "direct"\n'
        "[model_providers.direct]\n"
        'base_url = "http://127.0.0.1:18081/v1"\n',
    )
    monkeypatch.setenv("CODEX_CONFIG_DIR", str(tmp_path / "codex"))
    assert preflight_thinkthread_codex()["model"] == "gpt-5.6-luna"
