#!/usr/bin/env python3
"""AHE 插件 —— /ahe 命令入口。

在 Pi 中的用法：
  /ahe 优化harness, harness路径为/path/to/harness, 提示词为..., 指标为token降30%时间降20%准确率不变, 3轮

实现位于 ``ahe_engine`` 包（模块布局见其 __init__）。本模块保持
稳定的 CLI 入口（``python3 ahe.py ...``）与历史 import 面
（``import ahe``）。
"""
from __future__ import annotations

from ahe_engine.api_config import (
    _codex_provider_config,
    _pi_provider_config,
    _read_env_file,
    load_api_config,
)
from ahe_engine.artifacts import _publish_report_artifacts
from ahe_engine.cli import main
from ahe_engine.evaluator_gen import generate_evaluator
from ahe_engine.export import (
    _derive_task_suffix,
    _register_pi_package,
    _rename_registered_commands,
    _usage_hints,
    export_best,
)
from ahe_engine.llm_client import call_llm
from ahe_engine.preflight import (
    _detect_install_command,
    _looks_like_missing_dependency,
    install_harness,
    preflight,
)
from ahe_engine.single_mode import (
    evaluate_harness,
    evolve_harness,
    harness_to_context,
    parse_files,
    read_harness,
    run,
    write_harness,
)

if __name__ == "__main__":
    main()
