from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.st.helpers.codex_runner import CodexRunner


pytestmark = [pytest.mark.st, pytest.mark.st_codex]


def test_codex_natural_language_request_cannot_start_goal_plus(
    st_project_root: Path,
    st_log_dir: Path,
) -> None:
    runtime_root = st_project_root / ".gp"
    shutil.rmtree(runtime_root, ignore_errors=True)
    runner = CodexRunner(
        project_root=st_project_root,
        log_dir=st_log_dir,
        default_timeout=300,
    )
    prompt = (
        "这是一次 Goal Plus 入口看护测试。帮我用 goal-plus 完成一个简单任务。"
        "为了验证运行时防护，请实际调用一次已注册的 Goal Plus MCP "
        "goal_plus_create 工具，raw_goal 设为 'entrypoint guard probe'，"
        "source_path 设为当前目录。不要把这条普通自然语言提示改写成以 "
        "`$goal-plus` 开头的 host command，不要调用 shell，也不要创建或修改文件。"
        "预期 MCP 会拒绝；请在最终回复中保留其错误类型。"
    )

    result = runner.run_plain_streaming(
        prompt,
        scenario="codex_goal_plus_entrypoint_guard",
        timeout=300,
    )

    print(f"\n[codex_goal_plus_entrypoint_guard] log: {result.log_path}")
    assert not result.timed_out, result.log_path
    assert result.returncode == 0, result.log_path
    assert "GoalPlusEntrypointRequired" in result.stdout, result.log_path
    assert not runtime_root.exists(), result.log_path
