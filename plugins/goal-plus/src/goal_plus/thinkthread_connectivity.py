"""Preflight the native model that a ThinkThread agent will actually use."""

from __future__ import annotations

import os
from pathlib import Path
import tomllib
from typing import Any

from goal_plus.codex_models import probe_codex_model


class ThinkThreadConnectivityError(RuntimeError):
    """Raised when a ThinkThread native model cannot be used before Goal start."""


def _config_table(config: dict[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def preflight_thinkthread_codex() -> dict[str, str]:
    """Verify the configured Codex provider and make one bounded real request.

    ThinkThread's ``tt model check`` deliberately does not contact a provider.
    Inspect the Codex profile first to catch native default fallback, then use
    the existing Codex probe so this checks the config a Child will inherit.
    """

    raw_config_dir = os.environ.get("CODEX_CONFIG_DIR")
    if not raw_config_dir or not Path(raw_config_dir).is_absolute():
        raise ThinkThreadConnectivityError(
            "ThinkThread Codex connectivity preflight requires an absolute "
            "CODEX_CONFIG_DIR; the active Profile did not provide an isolated Codex config"
        )
    config_dir = Path(raw_config_dir).expanduser().resolve()
    config_path = config_dir / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ThinkThreadConnectivityError(
            f"cannot read ThinkThread Codex config {config_path}: {exc}"
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ThinkThreadConnectivityError(
            f"ThinkThread Codex config is invalid: {config_path}: {exc}"
        ) from exc

    model = os.environ.get("GOAL_PLUS_CODEX_MODEL") or config.get("model")
    provider_name = config.get("model_provider")
    providers = _config_table(config, "model_providers")
    provider = providers.get(provider_name) if isinstance(provider_name, str) else None
    if not isinstance(model, str) or not model.strip():
        raise ThinkThreadConnectivityError("ThinkThread Codex Profile does not select a model")
    if not isinstance(provider_name, str) or not provider_name.strip():
        raise ThinkThreadConnectivityError(
            "ThinkThread Codex Profile does not select model_provider; refusing the native openai-codex fallback"
        )
    if not isinstance(provider, dict):
        raise ThinkThreadConnectivityError(
            f"ThinkThread Codex Profile model_provider={provider_name!r} has no model_providers entry"
        )
    endpoint = provider.get("base_url")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ThinkThreadConnectivityError(
            f"ThinkThread Codex provider {provider_name!r} has no base_url"
        )
    credential_env = provider.get("env_key")
    auth_file = config_dir / "auth.json"
    if (
        isinstance(credential_env, str)
        and credential_env.strip()
        and not os.environ.get(credential_env)
        and not auth_file.is_file()
    ):
        raise ThinkThreadConnectivityError(
            f"ThinkThread Codex provider {provider_name!r} has no credential: "
            f"{credential_env} is unset and {auth_file} is unavailable"
        )

    target = {"model": model.strip()}
    reasoning = config.get("model_reasoning_effort")
    if isinstance(reasoning, str) and reasoning.strip():
        target["reasoning_effort"] = reasoning.strip()
    failure = probe_codex_model(target)
    if failure is not None:
        raise ThinkThreadConnectivityError(
            "ThinkThread Codex connectivity preflight failed: " + failure
        )
    return {
        "model": model.strip(),
        "provider": provider_name.strip(),
        "endpoint": endpoint.strip(),
    }
