"""Opt-in registration hook inherited by CANN Bench background child processes."""

from __future__ import annotations

import os


if os.environ.get("MUYUAN_CANNBENCH_AUTO_REGISTER") == "1":
    from muyuan_cannbench.registration import register_integrations

    register_integrations()
    if os.environ.get("MUYUAN_CANNBENCH_NON_NPU_EVAL") == "1":
        from muyuan_cannbench.smoke import install_non_npu_eval_patch

        install_non_npu_eval_patch()
