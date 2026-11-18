"""LLM API 配置发现。

优先级：显式参数 / LLM_* 环境变量、AHE_ENV_FILE、发起任务的 Pi 会话、
本机 Codex CLI 安装、内置本地 env 文件，最后回退 GLM-5.3 默认值。
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

def _pi_provider_config() -> tuple[str, str, str]:
    """从发起任务的 Pi 会话继承 provider 配置。

    Pi 会向 bash 子进程注入 PI_PROVIDER / PI_MODEL，provider 的
    baseUrl/apiKey 保存在 agent 目录的 models.json（auth.json 为备用
    凭证来源）。ahe.py 由此复用 /ahe 会话使用的同一 API，零手动配置。
    """
    provider = os.environ.get("PI_PROVIDER", "").strip()
    model = os.environ.get("PI_MODEL", "").strip()
    if not provider:
        return "", model, ""
    agent_dir = Path(os.environ.get("PI_CODING_AGENT_DIR") or
                     Path.home() / ".pi" / "agent")
    try:
        payload = json.loads((agent_dir / "models.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return "", model, ""
    entry = (payload.get("providers") or {}).get(provider)
    if not isinstance(entry, dict):
        return "", model, ""
    url = str(entry.get("baseUrl") or "")
    key = str(entry.get("apiKey") or "")
    if not key:
        try:
            auth = json.loads((agent_dir / "auth.json").read_text(encoding="utf-8-sig"))
            if isinstance(auth, dict):
                value = auth.get(provider)
                if isinstance(value, dict):
                    key = str(value.get("apiKey") or "")
                elif isinstance(value, str):
                    key = value
        except (OSError, ValueError):
            pass
    return url, key, model


def _codex_provider_config() -> tuple[str, str, str]:
    """从本机 Codex CLI 安装继承 provider 配置。

    Codex 将 API key 保存在 <codex home>/auth.json（OPENAI_API_KEY），
    模型与自定义 provider 端点保存在 config.toml（model、model_provider、
    [model_providers.<id>] base_url）。仅当回退链中更早的来源都未提供
    配置时才读取，因此在 Codex 中运行 /ahe 无需手动配置。
    """
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    url = key = model = ""
    try:
        auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8-sig"))
        if isinstance(auth, dict):
            key = str(auth.get("OPENAI_API_KEY") or "")
    except (OSError, ValueError):
        pass
    cfg = codex_home / "config.toml"
    if not cfg.is_file():
        return url, key, model
    text = cfg.read_text(encoding="utf-8-sig", errors="replace")
    match = re.search(r'^model\s*=\s*"([^"]+)"', text, re.M)
    if match:
        model = match.group(1)
    match = re.search(r'^model_provider\s*=\s*"([^"]+)"', text, re.M)
    if match:
        section = re.search(
            r"\[model_providers\." + re.escape(match.group(1)) + r"\]\s*(.*?)(?=^\[|\Z)",
            text, re.M | re.S)
        if section:
            base = re.search(r'^base_url\s*=\s*"([^"]+)"', section.group(1), re.M)
            if base:
                url = base.group(1)
    return url, key, model


def _read_env_file(env_file: Path, *, url: str, key: str, model: str) -> tuple[str, str, str]:
    if not env_file.is_file():
        return url, key, model
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.strip().split("=", 1)
            if k in ("LLM_BASE_URL", "CSI_BASE_URL") and not url: url = v
            if k in ("LLM_API_KEY", "CSI_API_KEY") and not key: key = v
            if k in ("LLM_MODEL", "CSI_MODEL") and not model: model = v
    return url, key, model


def load_api_config(api_url=None, api_key=None, model=None, evolve_model=None):
    """加载 LLM API 配置。

    优先级：显式参数 / LLM_* 环境变量，其次显式设置的 AHE_ENV_FILE，
    其次发起任务的 Pi 会话（PI_PROVIDER + models.json/auth.json），
    其次本机 Codex CLI（CODEX_HOME 或 ~/.codex：auth.json +
    config.toml），其次内置本地 env 文件，最后 GLM-5.3 默认值。
    """
    url = api_url or os.environ.get("LLM_BASE_URL", "")
    key = api_key or os.environ.get("LLM_API_KEY", "")
    model = model or os.environ.get("LLM_MODEL", "")
    evolve_model = evolve_model or os.environ.get("EVOLVE_LLM_MODEL", "")

    explicit_env_file = os.environ.get("AHE_ENV_FILE", "").strip()
    if (not url or not key) and explicit_env_file:
        url, key, model = _read_env_file(Path(explicit_env_file).expanduser(),
                                         url=url, key=key, model=model)

    if not url or not key:
        pi_url, pi_key, pi_model = _pi_provider_config()
        url = url or pi_url
        key = key or pi_key
        model = model or pi_model

    if not url or not key:
        cx_url, cx_key, cx_model = _codex_provider_config()
        url = url or cx_url
        key = key or cx_key
        model = model or cx_model

    return {"url": url, "key": key, "model": model or "GLM-5.3",
            "evolve_model": evolve_model or "GLM-5.3"}

