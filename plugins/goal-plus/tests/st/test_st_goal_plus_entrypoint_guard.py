from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.st.helpers.codex_runner import CodexRunner


pytestmark = [pytest.mark.st, pytest.mark.st_codex]


@pytest.mark.parametrize(
    ("requested_entrypoint", "scenario"),
    [
        ("goal-plus", "codex_goal_plus_entrypoint_guard"),
        (
            "goal-plus-with-final-check",
            "codex_goal_plus_final_check_entrypoint_guard",
        ),
    ],
    ids=["goal-plus", "goal-plus-with-final-check"],
)
def test_codex_natural_language_request_cannot_start_goal_plus(
    st_project_root: Path,
    st_log_dir: Path,
    requested_entrypoint: str,
    scenario: str,
) -> None:
    runtime_root = st_project_root / ".gp"
    shutil.rmtree(runtime_root, ignore_errors=True)
    runner = CodexRunner(
        project_root=st_project_root,
        log_dir=st_log_dir,
        default_timeout=300,
    )
    prompt = (
        f"这是一次 Goal Plus 入口看护测试。帮我用 {requested_entrypoint} 完成一个简单任务。"
        "为了验证运行时防护，请实际调用一次已注册的 Goal Plus MCP "
        "goal_plus_create 工具，raw_goal 设为 'entrypoint guard probe'，"
        "source_path 设为当前目录。不要把这条普通自然语言提示改写成以 "
        "`$goal-plus` 开头的 host command，不要调用 shell，也不要创建或修改文件。"
        "预期 MCP 会拒绝；请在最终回复中保留其错误类型。"
    )

    result = runner.run_plain_streaming(
        prompt,
        scenario=scenario,
        timeout=300,
    )

    print(f"\n[codex_goal_plus_entrypoint_guard] log: {result.log_path}")
    assert not result.timed_out, result.log_path
    assert result.returncode == 0, result.log_path
    assert "GoalPlusEntrypointRequired" in result.stdout, result.log_path
    assert not runtime_root.exists(), result.log_path


def test_codex_exact_host_command_can_start_and_complete_goal_plus(
    st_project_root: Path,
    st_log_dir: Path,
) -> None:
    runtime_root = st_project_root / ".gp"
    shutil.rmtree(runtime_root, ignore_errors=True)
    probe = st_project_root / "entrypoint-probe.txt"
    probe.write_text("exact-host-command\n", encoding="utf-8")
    runner = CodexRunner(
        project_root=st_project_root,
        log_dir=st_log_dir,
        default_timeout=300,
    )
    prompt = (
        "$goal-plus Read entrypoint-probe.txt and prove that its complete trimmed "
        "content is exactly exact-host-command. Do not edit any file. Use Goal Mode "
        "without Search or subagents. The host entrypoint has already created the "
        "Goal Plus record; never call goal_plus_create or goal_plus_update_goal. "
        "First record a high-confidence Goal Mode triage. Create one required "
        "Read and verify the file, perform the final raw-goal audit, and call "
        "goal_plus_set_status with status=complete and concrete file evidence."
    )

    result = runner.run_streaming(
        prompt,
        scenario="codex_goal_plus_exact_entrypoint",
        timeout=300,
    )

    print(f"\n[codex_goal_plus_exact_entrypoint] log: {result.log_path}")
    assert not result.timed_out, result.log_path
    assert result.returncode == 0, result.log_path
    goal_files = sorted((runtime_root / "goal-plus").glob("gp_*/goal.json"))
    assert len(goal_files) == 1, result.log_path
    record = json.loads(goal_files[0].read_text(encoding="utf-8"))
    assert record["status"] == "complete", result.log_path
    assert record["entrypoint_status"] == "host_verified", result.log_path
    assert len(record["host_command_invocations"]) == 1, result.log_path
    invocation = record["host_command_invocations"][0]
    assert invocation["host"] == "codex", result.log_path
    assert invocation["action"] == "start", result.log_path
    assert record["triage"]["recommended_phase"] == "goal", result.log_path
    assert record["work_items"] == [], result.log_path
