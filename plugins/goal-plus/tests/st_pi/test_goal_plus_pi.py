from __future__ import annotations

import json
import os
import signal
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from goal_plus.monitor import goal_plus_monitor_snapshot


ROOT = Path(__file__).resolve().parents[2]
INSTALL_HINT = (
    'Run ./install.sh --pi, or select a prepared pi-goal-plus launcher with ST_PI_BINARY.'
)
AUDIT_PROMPT = (
    "请只读审计当前分支的 Pi native Goal Plus 改造是否完整。范围限定为 "
    "assets/pi/extensions/goal-plus.ts、assets/goal-plus-instructions.txt、docs/pi.md、"
    "tests/test_pi_assets.py。重点检查：/goal-plus 命令、session_start 状态恢复、"
    "before_agent_start 上下文注入、tool_call pre-tool gate、agent_end stop gate、"
    "terminal stats 输出。不要修改文件。最后给出结论，并把 Goal Plus 状态设为 complete。"
)
STOP_GATE_INTERCEPTION_PROMPT = (
    "验证 native Pi Goal Plus 的 Stop gate 会拦截尚未完成的 Goal。不要修改任何文件，"
    "不要进入 Search Mode，也不要启动 worker。严格分成两个 assistant turn：第一轮直接"
    "输出精确标记 FIRST_TURN_ATTEMPTED_STOP；第一轮禁止调用 "
    "goal_plus_set_status，并在输出标记后正常结束，让 Goal 保持 active。收到 Goal Plus 自动"
    "发送的 stop continuation 后，第二轮确认 README.md 存在，调用 goal_plus_set_status 将"
    "状态设为 complete，并输出精确标记 SECOND_TURN_AFTER_HOOK。"
)
AUTONOMOUS_OPTIMIZATION_PROMPT = (
    "优化 examples/edgebench-ad-placement/workspace-template 中的广告矩形布局程序："
    "在保持所有公开案例输出合法的前提下，尽量提高综合得分。"
    "先自行检查工作区，找出可重复的评估方法、优化指标和安全的修改边界，"
    "再由 Goal Plus 自主选择合适的执行方式，不要等待额外的用户确认。"
    "这是一个低成本冒烟验证：最多探索 1 个候选，并行度为 1，单个候选最多运行 "
    "60 秒和 4 轮。只允许修改 solution.cpp，不要修改工作区中的其他文件。"
    "完成后提升最佳的可验证结果并报告最终得分，但不要把 promotion patch 应用回只读的 "
    "workspace-template；最后把 Goal Plus 状态设为 complete。"
)
MULTI_SEARCH_PROMPT = (
    "在同一个 Goal Plus 任务里顺序运行两个最小但完整的 Pi search task。两次都读取 "
    "examples/edgebench-ad-placement/search-spec.json，并冻结其中的 "
    ".goal-plus-verifiers/ad_local_score.py、tools/bin/gen、tools/bin/tester。"
    "第一个 SearchSpec 使用 strategy.name=random，第二个使用 strategy.name=agent_guided；"
    "两次都设置 orchestration_mode=parallel_loops、"
    "max_parallel=1 且不设置已弃用的 max_parallel，worker budget 为 "
    "max_runtime_seconds=60、max_turns=4、"
    "on_exceed=interrupt，只允许候选修改 solution.cpp。每次都必须分别完成 "
    "goal_plus_search_freeze_spec、goal_plus_search_create、goal_plus_link_search_run、candidate verifier、"
    "selection、report、promotion 和 goal_plus_record_search_result。第一次结果记录后不要"
    "结束 Goal Plus，继续创建并链接第二个不同 run_id。只有两个 search task 都完成后，"
    "才把 Goal Plus 状态设为 complete。"
)
SPEC_REVISION_FIRST_PROMPT = (
    "在同一个 Goal Plus 记录中验证一次真实的两版 frozen spec 流程。工作区是 {workspace}。"
    "第一轮只使用 .goal-plus-verifiers/simple_score.py，metric_name=score、maximize，"
    "只允许修改 candidate.txt；使用 random、"
    "orchestration_mode=parallel_loops、max_parallel=1 且不设置已弃用的 max_parallel，worker budget "
    "为 max_runtime_seconds=60、max_turns=4、on_exceed=interrupt。完成 freeze、create、"
    "link、candidate verify、select、promote 和 record_search_result，但不要生成 report。"
    "拿到第一轮真实 verifier 结果后，不要结束 Goal Plus；在现有 raw-goal audit 中选择 "
    "revise_goal。不要调用 goal_plus_update_goal；把状态设为 needs_user，next action 要求用户"
    "使用精确 /goal-plus edit，然后停止。"
)
SPEC_REVISION_SECOND_GOAL = (
    "继续同一个 Goal Plus 记录，工作区仍是 {workspace}。使用 "
    ".goal-plus-verifiers/strict_score.py 在同一 candidate.txt 上最大化 score，"
    "并说明第一版只能证明相对改进，不能证明已经达到更深目标。随后保存"
    "新的高置信 spec draft、冻结新的 frozen spec，并以相同的 Pi 单候选预算完成第二个"
    "完整 Search run。两轮必须使用不同 frozen_spec_id 和 run_id；第二轮完成后才把"
    " Goal Plus 状态设为 complete。"
)


def _write_spec_revision_workspace(workspace: Path) -> None:
    verifier_dir = workspace / ".goal-plus-verifiers"
    verifier_dir.mkdir(parents=True)
    workspace.joinpath("candidate.txt").write_text("0\n", encoding="utf-8")
    for name, cap in (("simple_score.py", 10), ("strict_score.py", 100)):
        verifier_dir.joinpath(name).write_text(
            "from __future__ import annotations\n"
            "import json\n"
            "from pathlib import Path\n"
            f"CAP = {cap}\n"
            "value = int(Path('candidate.txt').read_text(encoding='utf-8').strip())\n"
            "print(json.dumps({'score': min(value, CAP)}))\n",
            encoding="utf-8",
        )


def _pi_base_command(session_dir: Path, session_id: str) -> list[str]:
    command = [
        os.environ.get("ST_PI_BINARY", "pi"),
        "--approve",
        "--session-dir",
        str(session_dir),
        "--session-id",
        session_id,
    ]
    model = os.environ.get("ST_PI_MODEL")
    if model:
        command.extend(["--model", model])
    thinking = os.environ.get("ST_PI_THINKING")
    if thinking:
        command.extend(["--thinking", thinking])
    return command


def _pi_resume_command(
    session_dir: Path,
    session_id: str,
    selector: str,
) -> list[str]:
    command = _pi_base_command(session_dir, session_id)
    session_id_index = command.index("--session-id")
    del command[session_id_index : session_id_index + 2]
    if selector == "session":
        command.extend(["--session", session_id])
    elif selector == "continue":
        command.append("--continue")
    elif selector == "resume":
        command.append("--resume")
    else:
        raise ValueError(f"unknown Pi resume selector: {selector}")
    return command


def _run_env(search_root: Path) -> dict[str, str]:
    return {
        **os.environ,
        "GOAL_PLUS_ROOT": str(search_root),
    }


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _session_file(session_dir: Path) -> Path:
    files = sorted(session_dir.glob("*.jsonl"), key=lambda path: path.stat().st_mtime)
    assert files, f"no Pi session JSONL found in {session_dir}"
    return files[-1]


def _goal_record(search_root: Path) -> dict:
    goal_files = sorted((search_root / "goal-plus").glob("gp_*/goal.json"))
    assert len(goal_files) == 1
    return json.loads(goal_files[0].read_text(encoding="utf-8"))


def _goal_events(search_root: Path, goal_plus_id: str) -> list[dict]:
    return _read_jsonl(search_root / "goal-plus" / goal_plus_id / "events.jsonl")


def _tool_calls(entries: list[dict], name: str) -> list[dict]:
    calls = []
    for entry in entries:
        message = entry.get("message") or {}
        if message.get("role") != "assistant":
            continue
        for item in message.get("content") or []:
            if item.get("type") == "toolCall" and item.get("name") == name:
                calls.append(item)
    return calls


def _assert_goal_plus_jsonl_is_clean(session_path: Path) -> None:
    session_text = session_path.read_text(encoding="utf-8")
    assert "unexpected keyword argument" not in session_text
    assert INSTALL_HINT not in session_text

    entries = _read_jsonl(session_path)
    triage_calls = _tool_calls(entries, "goal_plus_record_triage")
    for call in triage_calls:
        arguments = call.get("arguments") or {}
        assert "triage" in arguments
        assert "classification" not in arguments
        assert "reason" not in arguments


@pytest.mark.st_pi
@pytest.mark.parametrize(
    "requested_entrypoint",
    ["goal-plus", "goal-plus-with-final-check"],
)
def test_goal_plus_plain_prompt_cannot_start_goal_plus(
    st_pi_run_root: Path,
    requested_entrypoint: str,
) -> None:
    search_root = st_pi_run_root / ".gp"
    session_dir = st_pi_run_root / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    command = [
        *_pi_base_command(
            session_dir,
            f"st-pi-{requested_entrypoint}-entrypoint-guard",
        ),
        "-p",
        (
            "这是一次普通自然语言的 Goal Plus 入口看护测试。"
            f"帮我用 {requested_entrypoint} "
            "完成一个简单任务。为了验证 extension 的运行时防护，请实际调用一次 "
            "goal_plus_record_triage 工具，goal_plus_id 设为 gp_entrypoint_guard，"
            "triage 使用非优化、高置信、recommended_phase=goal。不要把这条提示改写成 "
            "以 /goal-plus 开头的命令，不要调用 shell，也不要创建或修改文件。"
            "预期工具调用会被拒绝；最终回复保留错误类型。"
        ),
    ]

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=_run_env(search_root),
        text=True,
        capture_output=True,
        timeout=int(os.environ.get("ST_PI_TIMEOUT", "300")),
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:] or result.stdout[-2000:]
    session_path = _session_file(session_dir)
    session_text = session_path.read_text(encoding="utf-8")
    assert "GoalPlusEntrypointRequired" in session_text
    assert not search_root.exists()


@pytest.mark.st_pi
def test_goal_plus_exact_command_can_start_and_complete_goal_plus(
    st_pi_run_root: Path,
) -> None:
    search_root = st_pi_run_root / ".gp"
    session_dir = st_pi_run_root / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    probe = st_pi_run_root / "entrypoint-probe.txt"
    probe.write_text("exact-pi-host-command\n", encoding="utf-8")
    command = [
        *_pi_base_command(session_dir, "st-pi-goal-plus-exact-entrypoint"),
        "-p",
        (
            "/goal-plus 在 300 秒内只读检查 "
            f"{probe}，证明其完整 trimmed content 恰好是 exact-pi-host-command。"
            "不要修改任何文件；使用 Goal Mode，不要进入 Search，不要启动 subagent。"
            "原生 Pi host entrypoint 已经创建记录；不要调用 goal_plus_create 或 "
            "goal_plus_update_goal。先记录高置信 Goal Mode triage。完成原始目标审计后，"
            "使用具体文件证据调用 goal_plus_set_status(status=complete)。"
        ),
    ]

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=_run_env(search_root),
        text=True,
        capture_output=True,
        timeout=int(os.environ.get("ST_PI_TIMEOUT", "300")),
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:] or result.stdout[-2000:]
    record = _goal_record(search_root)
    assert record["status"] == "complete"
    assert record["entrypoint_status"] == "host_verified"
    assert len(record["host_command_invocations"]) == 1
    invocation = record["host_command_invocations"][0]
    assert invocation["agent_harness"] == "pi"
    assert invocation["transport"] == "pi_extension_command"
    assert invocation["command"] == "/goal-plus"
    assert invocation["action"] == "start"
    assert record["triage"]["recommended_phase"] == "goal"
    assert record["work_items"] == []
    _assert_goal_plus_jsonl_is_clean(_session_file(session_dir))


def _run_native_tui_resume(
    command: list[str],
    search_root: Path,
    log_path: Path,
    *,
    select_from_picker: bool = False,
) -> None:
    if os.name != "posix":
        pytest.skip("native TUI resume ST requires a POSIX PTY")
    import fcntl
    import pty
    import select
    import struct
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 35, 120, 0, 0))
    process = subprocess.Popen(
        command, cwd=ROOT, env={**_run_env(search_root), "TERM": "xterm-256color"},
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
    )
    os.close(slave)
    picker_selected = not select_from_picker
    output = bytearray()
    try:
        with log_path.open("wb") as log:
            deadline = time.monotonic() + int(os.environ.get("ST_PI_TIMEOUT", "300"))
            while time.monotonic() < deadline:
                assert process.poll() is None, f"Pi TUI exited; see {log_path}"
                if select.select([master], [], [], 0.1)[0]:
                    chunk = os.read(master, 65536)
                    output.extend(chunk)
                    log.write(chunk)
                    log.flush()
                if (
                    not picker_selected
                    and b"Resume Session (Current Folder)" in output
                    and b"goal_plus_id:" in output
                ):
                    os.write(master, b"\r")
                    picker_selected = True
                if _goal_record(search_root)["status"] == "complete":
                    assert picker_selected
                    return
            pytest.fail(f"Pi TUI did not complete the resumed Goal; see {log_path}")
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        os.close(master)


@pytest.mark.st_pi
@pytest.mark.parametrize(
    "interruption,resume_mode,selector",
    [
        pytest.param("process_loss", "json", "session", id="session-json-process-loss"),
        pytest.param("user_abort", "json", "session", id="session-json-user-abort"),
        pytest.param("process_loss", "tui_prompt", "session", id="session-tui-prompt"),
        pytest.param("process_loss", "json", "continue", id="continue-json"),
        pytest.param("process_loss", "tui_prompt", "resume", id="resume-picker-tui"),
    ],
)
def test_goal_plus_native_session_resume(
    st_pi_run_root: Path,
    interruption: str,
    resume_mode: str,
    selector: str,
) -> None:
    search_root = st_pi_run_root / ".gp"
    session_dir = st_pi_run_root / "sessions"
    session_dir.mkdir()
    session_id = "st-pi-native-resume"
    probe = st_pi_run_root / "resume-probe.txt"
    log_path = st_pi_run_root / "initial.log"
    prompt = (
        f"/goal-plus 在 300 秒内只读验证 {probe} 的完整 trimmed content 是 native-resume-ok。"
        "不要修改文件，不要进入 Search 或启动 worker。先记录高置信 Goal Mode triage。"
        "测试控制器会在宿主进程重启后创建该文件；文件出现前保持 active，不要记录终态或"
        "needs_user。原生 session 恢复后，读取文件并用具体证据完成原始目标审计，调用 "
        "goal_plus_set_status(status=complete)。"
    )
    command = _pi_base_command(session_dir, session_id)
    if interruption == "user_abort":
        command.extend(["--mode", "rpc"])
    else:
        command.extend(["-p", "--mode", "json", prompt])
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=_run_env(search_root),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if interruption == "user_abort" else subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        try:
            if interruption == "user_abort":
                process.stdin.write(json.dumps({"type": "prompt", "message": prompt}) + "\n")
                process.stdin.flush()
            deadline = time.monotonic() + int(os.environ.get("ST_PI_TIMEOUT", "300"))
            while time.monotonic() < deadline:
                assert process.poll() is None, log_path.read_text(encoding="utf-8")[-4000:]
                goal_files = list((search_root / "goal-plus").glob("gp_*/goal.json"))
                if goal_files:
                    record = _goal_record(search_root)
                    if record.get("triage") and list(session_dir.glob("*.jsonl")):
                        break
                time.sleep(0.1)
            else:
                pytest.fail(f"Pi did not persist triage before timeout; see {log_path}")
            if interruption == "user_abort":
                process.stdin.write(json.dumps({"type": "abort"}) + "\n")
                process.stdin.flush()
                pause_deadline = time.monotonic() + 30
                while time.monotonic() < pause_deadline:
                    if _goal_record(search_root).get("control", {}).get("state") == "paused":
                        break
                    assert process.poll() is None, log_path
                    time.sleep(0.1)
                else:
                    pytest.fail(f"Pi abort did not pause the binding; see {log_path}")
        finally:
            if process.poll() is None:
                # Stop only this test-owned process tree after durable evidence.
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)

    before = _goal_record(search_root)
    assert before["status"] == "active"
    assert before["active_session"]["session_id"] == session_id
    expected_state = "attached"
    assert before["active_session"]["state"] == expected_state
    resume_command = _pi_resume_command(session_dir, session_id, selector)
    print_resume_command = [*resume_command, "-p", "--mode", "json"]

    # Print mode without a user prompt restores the session but must not race
    # a fire-and-forget model turn against CLI shutdown.
    if selector != "resume":
        empty = subprocess.run(
            print_resume_command,
            cwd=ROOT,
            env=_run_env(search_root),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert empty.returncode == 0, empty.stderr[-3000:]
        assert '"type":"agent_start"' not in empty.stdout
        assert _goal_record(search_root)["status"] == "active"
        assert _goal_record(search_root)["active_session"]["state"] == expected_state

    probe.write_text("native-resume-ok\n", encoding="utf-8")
    if selector != "resume":
        chat = subprocess.run(
            [*print_resume_command, "Only answer this unrelated chat: what is 2 + 2? Do not resume the stopped task."],
            cwd=ROOT, env=_run_env(search_root), text=True, capture_output=True, timeout=120,
        )
        (st_pi_run_root / "stopped-chat.log").write_text(chat.stdout + chat.stderr)
        assert chat.returncode == 0
        assert _goal_record(search_root)["control"]["state"] == "paused"
    continuation = "/goal-plus resume"
    if resume_mode.startswith("tui"):
        _run_native_tui_resume(
            [*resume_command, *([continuation] if resume_mode == "tui_prompt" else [])],
            search_root,
            st_pi_run_root / "resumed.log",
            select_from_picker=selector == "resume",
        )
    else:
        resumed = subprocess.run(
            [*print_resume_command, continuation],
            cwd=ROOT,
            env=_run_env(search_root),
            text=True,
            capture_output=True,
            timeout=int(os.environ.get("ST_PI_TIMEOUT", "300")),
            check=False,
        )
        (st_pi_run_root / "resumed.log").write_text(
            resumed.stdout + resumed.stderr, encoding="utf-8"
        )
        assert resumed.returncode == 0, resumed.stderr[-3000:] or resumed.stdout[-3000:]
    after = _goal_record(search_root)
    assert after["status"] == "complete"
    assert after["goal_plus_id"] == before["goal_plus_id"]
    assert after["goal_revision"] == before["goal_revision"]
    assert [item["action"] for item in after["host_command_invocations"]] == ["start", "resume"]
    assert after["active_session"]["session_id"] == session_id
    decisions = [
        event["payload"]
        for event in _goal_events(search_root, after["goal_plus_id"])
        if event["event_type"] == "session_continuation_decided"
    ]
    assert not any(item["decision"] == "continue_now" for item in decisions)


@pytest.mark.st_pi
@pytest.mark.st
def test_goal_plus_active_rpc_steering_continues_without_resume(st_pi_run_root: Path) -> None:
    search_root = st_pi_run_root / ".gp"
    session_dir = st_pi_run_root / "sessions"
    session_dir.mkdir()
    probe = st_pi_run_root / "steering-probe.txt"
    log_path = st_pi_run_root / "steering.log"
    prompt = (
        f"/goal-plus Within 300 seconds record high-confidence Goal Mode triage, then wait for {probe} "
        "to appear and verify its trimmed content is native-steering-ok. Do not write files, use Search "
        "or subagents. Keep active until the controller creates the file; then complete with concrete evidence."
    )
    with log_path.open("w") as log:
        process = subprocess.Popen([*_pi_base_command(session_dir, "st-pi-steering"), "--mode", "rpc"],
            cwd=ROOT, env=_run_env(search_root), stdin=subprocess.PIPE, stdout=log,
            stderr=subprocess.STDOUT, text=True, start_new_session=True)
        try:
            process.stdin.write(json.dumps({"type": "prompt", "message": prompt}) + "\n")
            process.stdin.flush()
            sent = False
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                assert process.poll() is None, log_path
                if list((search_root / "goal-plus").glob("gp_*/goal.json")):
                    record = _goal_record(search_root)
                    assert record["control"]["state"] != "paused", f"Steering paused the task; see {log_path}"
                    if record.get("triage") and not sent:
                        process.stdin.write(json.dumps({"type": "steer", "message": "New thought: keep the original task and include STEERING_SEEN in its completion evidence."}) + "\n")
                        process.stdin.flush()
                        probe.write_text("native-steering-ok\n")
                        sent = True
                    if record["status"] == "complete":
                        assert sent
                        assert [i["action"] for i in record["host_command_invocations"]] == ["start"]
                        assert "STEERING_SEEN" in json.dumps(_goal_events(search_root, record["goal_plus_id"]))
                        return
                time.sleep(0.1)
            pytest.fail(f"Active steering did not finish; see {log_path}")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


@pytest.mark.st_pi
def test_goal_plus_busy_pause_duplicate_resume_and_clear(st_pi_run_root: Path) -> None:
    search_root = st_pi_run_root / ".gp"
    session_dir = st_pi_run_root / "sessions"
    session_dir.mkdir()
    log_path = st_pi_run_root / "busy-controls.log"
    with log_path.open("w") as log:
        process = subprocess.Popen([*_pi_base_command(session_dir, "st-pi-busy-controls"), "--mode", "rpc"],
            cwd=ROOT, env=_run_env(search_root), stdin=subprocess.PIPE, stdout=log,
            stderr=subprocess.STDOUT, text=True, start_new_session=True)
        def send(message):
            process.stdin.write(json.dumps({"type": "prompt", "message": message}) + "\n")
            process.stdin.flush()
        def wait_for(predicate, seconds=60):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                assert process.poll() is None, log_path
                paths = list((search_root / "goal-plus").glob("gp_*/goal.json"))
                if paths:
                    record = _goal_record(search_root)
                    if predicate(record):
                        return record
                time.sleep(0.1)
            pytest.fail(f"Busy control transition timed out; see {log_path}")
        try:
            send(f"/goal-plus Within 300 seconds record high-confidence Goal Mode triage, then keep checking {st_pi_run_root / 'controller-probe.txt'} until it appears. Do not write files, run Search or subagents. Do not poll longer than one second per tool. Keep active; this controller will send commands during the task.")
            wait_for(lambda r: r.get("triage"), seconds=300)
            send("/goal-plus pause")
            paused = wait_for(lambda r: r["control"]["reason"] == "user_pause")
            assert process.poll() is None  # pause did not kill the current native execution.
            send("/goal-plus resume")
            resumed = wait_for(lambda r: r["control"]["state"] == "active")
            send("/goal-plus resume")
            duplicate = wait_for(lambda r: len([i for i in r["host_command_invocations"] if i["action"] == "resume"]) >= 2)
            assert duplicate["control"]["version"] == resumed["control"]["version"] == paused["control"]["version"] + 1
            send("/goal-plus clear")
            cleared = wait_for(lambda r: r["control"]["state"] == "closed")
            assert cleared["status"] == "abandoned"
            assert cleared["goal_revision"] == 1
            assert process.poll() is None
            # End only the test's current turn, then prove normal chat cannot
            # revive the cleared task or replay its queued continuation.
            process.stdin.write(json.dumps({"type": "abort"}) + "\n")
            process.stdin.flush()
            abort_deadline = time.monotonic() + 60
            while time.monotonic() < abort_deadline:
                events = [json.loads(line) for line in log_path.read_text().splitlines() if line.startswith("{")]
                if any(e.get("type") == "response" and e.get("command") == "abort" and e.get("success") for e in events):
                    break
                time.sleep(0.1)
            else:
                pytest.fail(f"Native abort did not settle; see {log_path}")
            send("Reply exactly CHAT_AFTER_CLEAR. Do not call any tools.")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if '"text":"CHAT_AFTER_CLEAR"' in log_path.read_text().replace(': ', ':'):
                    assert _goal_record(search_root)["control"] == cleared["control"]
                    entries = [json.loads(line) for path in session_dir.glob("*.jsonl") for line in path.read_text().splitlines() if line.startswith("{")]
                    main_requests = [entry for entry in entries if (entry.get("message") or {}).get("customType") == "goal-plus-command-context"]
                    assert len(main_requests) <= 2  # initial Main plus at most one idle resume
                    return
                time.sleep(0.1)
            pytest.fail(f"Chat after clear did not settle; see {log_path}")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


@pytest.mark.st_pi
def test_goal_plus_print_prompt_completes_without_triage(
    st_pi_run_root: Path,
) -> None:
    search_root = st_pi_run_root / ".gp"
    session_dir = st_pi_run_root / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_id = "st-pi-goal-plus-print"
    command = [
        *_pi_base_command(session_dir, session_id),
        "-p",
        f"/goal-plus {AUDIT_PROMPT}",
    ]

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=_run_env(search_root),
        text=True,
        capture_output=True,
        timeout=int(os.environ.get("ST_PI_TIMEOUT", "420")),
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:] or result.stdout[-2000:]
    record = _goal_record(search_root)
    assert record["status"] == "complete"
    events = _goal_events(search_root, record["goal_plus_id"])
    event_types = [event["event_type"] for event in events]
    assert event_types[0] == "created"
    assert "triage_recorded" not in event_types
    assert "status_changed" in event_types
    assert "spec_draft_saved" not in event_types
    assert "search_linked" not in event_types
    assert record["phase"] == "goal"
    assert record["triage"] is None
    _assert_goal_plus_jsonl_is_clean(_session_file(session_dir))


@pytest.mark.st_pi
def test_goal_plus_print_stop_gate_intercepts_early_finish_and_continues(
    st_pi_run_root: Path,
) -> None:
    search_root = st_pi_run_root / ".gp"
    session_dir = st_pi_run_root / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    command = [
        *_pi_base_command(session_dir, "st-pi-goal-plus-stop-gate"),
        "-p",
        f"/goal-plus {STOP_GATE_INTERCEPTION_PROMPT}",
    ]

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=_run_env(search_root),
        text=True,
        capture_output=True,
        timeout=int(os.environ.get("ST_PI_TIMEOUT", "420")),
        check=False,
    )

    assert result.returncode == 0, result.stderr[-3000:] or result.stdout[-3000:]
    record = _goal_record(search_root)
    assert record["status"] == "complete"

    session_path = _session_file(session_dir)
    _assert_goal_plus_jsonl_is_clean(session_path)
    entries = _read_jsonl(session_path)
    continuation_indexes = [
        index
        for index, entry in enumerate(entries)
        if entry.get("customType") == "goal-plus-stop-continuation"
    ]
    assert continuation_indexes, "Pi agent_end did not persist a stop continuation"
    continuation_index = continuation_indexes[0]

    before_continuation = entries[:continuation_index]
    after_continuation = entries[continuation_index + 1 :]
    before_text = json.dumps(before_continuation, ensure_ascii=False)
    after_text = json.dumps(after_continuation, ensure_ascii=False)
    assert "FIRST_TURN_ATTEMPTED_STOP" in before_text
    assert "SECOND_TURN_AFTER_HOOK" in after_text
    assert not _tool_calls(before_continuation, "goal_plus_set_status")

    completion_calls = _tool_calls(after_continuation, "goal_plus_set_status")
    assert completion_calls
    assert completion_calls[-1]["arguments"]["status"] == "complete"

    events = _goal_events(search_root, record["goal_plus_id"])
    event_types = [event["event_type"] for event in events]
    assert "gate_blocked" in event_types
    assert event_types.index("gate_blocked") < event_types.index("status_changed")


@pytest.mark.st_pi
def test_goal_plus_with_final_check_runs_pi_reviewer(
    st_pi_run_root: Path,
) -> None:
    search_root = st_pi_run_root / ".gp"
    session_dir = st_pi_run_root / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    command = [
        *_pi_base_command(session_dir, "st-pi-goal-plus-final-check"),
        "-p",
        (
            "/goal-plus-with-final-check Read pyproject.toml and prove that the project "
            "name is goal-plus. Do not edit any file. Use Goal Mode and finish only through "
            "the independent Pi final reviewer."
        ),
    ]

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=_run_env(search_root),
        text=True,
        capture_output=True,
        timeout=int(os.environ.get("ST_PI_FINAL_CHECK_TIMEOUT", "600")),
        check=False,
    )

    assert result.returncode == 0, result.stderr[-3000:] or result.stdout[-3000:]
    record = _goal_record(search_root)
    assert record["status"] == "complete"
    assert record["entrypoint_status"] == "host_verified"
    assert len(record["host_command_invocations"]) == 1
    invocation = record["host_command_invocations"][0]
    assert invocation["agent_harness"] == "pi"
    assert invocation["transport"] == "pi_extension_command"
    assert invocation["command"] == "/goal-plus-with-final-check"
    assert invocation["action"] == "start"
    assert record["policy"]["final_check"]["mode"] == "required"
    assert record["goal_revision"] == 1
    assert record["final_checks"][-1]["checker_harness"] == "pi"
    assert record["final_checks"][-1]["status"] == "passed"
    event_types = [
        event["event_type"]
        for event in _goal_events(search_root, record["goal_plus_id"])
    ]
    assert "final_check_requested" in event_types
    assert "final_check_submitted" in event_types


@pytest.mark.st_pi
def test_goal_plus_print_autonomously_enters_search(
    st_pi_run_root: Path,
) -> None:
    from goal_plus.host_scope import close_execution_scope, execution_scope_for_run
    from tests._runtime_helpers import git_commit_all

    source = st_pi_run_root / "source"
    shutil.copytree(
        ROOT / "examples" / "edgebench-ad-placement" / "workspace-template",
        source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".tmp"),
    )
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    git_commit_all(source, "isolated native Search baseline")
    search_root = st_pi_run_root / ".gp"
    session_dir = st_pi_run_root / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    command = [
        *_pi_base_command(session_dir, "st-pi-goal-plus-autonomous-search"),
        "-p",
        "/goal-plus " + AUTONOMOUS_OPTIMIZATION_PROMPT.replace(
            "examples/edgebench-ad-placement/workspace-template", str(source)
        ),
    ]
    env = _run_env(search_root)
    env["GOAL_PLUS_SOURCE_PATH"] = str(source)
    env["PYTHONPATH"] = str(ROOT / "src")
    print(f"Native Search ST: {st_pi_run_root}", flush=True)
    try:
        result = subprocess.run(
            command,
            cwd=source,
            env=env,
            text=True,
            capture_output=True,
            timeout=int(os.environ.get("ST_PI_SEARCH_TIMEOUT", "600")),
            check=False,
        )
    finally:
        for run_path in (search_root / "runs").glob("*/run.json"):
            scope = execution_scope_for_run(root_dir=search_root, run_id=run_path.parent.name)
            if scope is not None:
                closed = close_execution_scope(
                    root_dir=search_root, scope_id=scope["scope_id"],
                    reason="native_search_st_cleanup", timeout_seconds=15,
                )
                assert closed["state"] == "closed", closed

    assert result.returncode == 0, result.stderr[-2000:] or result.stdout[-2000:]
    record = _goal_record(search_root)
    assert record["status"] == "complete"
    events = _goal_events(search_root, record["goal_plus_id"])
    event_types = [event["event_type"] for event in events]
    assert "spec_draft_saved" in event_types
    assert "frozen_verifier_confirmed" not in event_types
    assert "search_linked" in event_types
    draft = next(
        event["payload"]
        for event in events
        if event["event_type"] == "spec_draft_saved"
    )
    assert draft["confidence"] == "high"
    assert draft.get("open_questions") == []
    assert "user_confirmed_frozen_verifier" not in draft
    linked = record.get("linked_search") or {}
    assert linked.get("run_id"), record
    assert linked.get("selected_candidate_id"), record
    assert linked.get("report_path"), record
    assert linked.get("promotion_artifact_path"), record
    assert Path(linked["report_path"]).exists()
    assert Path(linked["promotion_artifact_path"]).exists()
    run_record = json.loads(
        (search_root / "runs" / linked["run_id"] / "run.json").read_text(encoding="utf-8")
    )
    assert run_record["state"] == "promoted"
    frozen = json.loads(
        (search_root / "specs" / run_record["frozen_spec_id"] / "frozen_spec.json").read_text()
    )
    assert frozen["agent_harness"] == "pi"
    assert "worker_host" not in frozen["spec"]["strategy"]
    worker_sessions = sorted(
        (search_root / "runs" / linked["run_id"] / "agent_sessions").glob("agent_*.json")
    )
    assert worker_sessions
    for session_path in worker_sessions:
        session = json.loads(session_path.read_text(encoding="utf-8"))
        assert session["agent_harness"] == "pi"
        metadata = session["session_handle"]["metadata"]
        assert metadata["continuation"] == "native_session"
        assert metadata.get("runner_failed") is not True
        assert metadata["raw_logging"] is False
        assert metadata["session_file"]
        assert Path(metadata["session_file"]).is_file()
        assert metadata["text_log"] is None
        assert Path(metadata["event_log"]).exists()
    assert (search_root / "host-sessions" / "pi").is_dir()
    _assert_goal_plus_jsonl_is_clean(_session_file(session_dir))


@pytest.mark.st_pi
def test_circle_packing_git_common_agent_apply(
    st_pi_run_root: Path,
) -> None:
    examples_root = st_pi_run_root / "examples"
    source_examples = ROOT / "examples"
    shutil.copytree(
        source_examples / "circle-packing",
        examples_root / "circle-packing",
        ignore=shutil.ignore_patterns(
            ".gp", "workspace", "__pycache__", "*.pyc", ".tmp"
        ),
    )
    example = examples_root / "circle-packing-git-apply"
    shutil.copytree(
        source_examples / "circle-packing-git-apply",
        example,
        ignore=shutil.ignore_patterns(
            ".gp", "workspace", "__pycache__", "*.pyc", ".tmp"
        ),
    )
    prepared = subprocess.run(
        ["python", "prepare_workspace.py"],
        cwd=example,
        text=True,
        capture_output=True,
        check=False,
    )
    assert prepared.returncode == 0, prepared.stderr or prepared.stdout

    search_root = example / ".gp"
    session_dir = st_pi_run_root / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    prompt = Path(__file__).with_name("circle-common-prompt.txt").read_text(encoding="utf-8")
    command = [
        *_pi_base_command(session_dir, "st-pi-circle-git-common"),
        "-p",
        prompt,
    ]
    env = _run_env(search_root)
    env["GOAL_PLUS_SOURCE_PATH"] = str(example)
    result = subprocess.run(
        command,
        cwd=example,
        env=env,
        text=True,
        capture_output=True,
        timeout=int(os.environ.get("ST_PI_SEARCH_TIMEOUT", "1200")),
        check=False,
    )

    (st_pi_run_root / "circle.stdout.log").write_text(result.stdout, encoding="utf-8")
    (st_pi_run_root / "circle.stderr.log").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stderr[-4000:] or result.stdout[-4000:]
    record = _goal_record(search_root)
    assert record["status"] == "complete"
    linked = record["linked_search"]
    run_id = linked["run_id"]
    run_dir = search_root / "runs" / run_id
    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_record["state"] == "promoted"
    assert run_record["promotion"]["state"] == "applied"
    assert run_record["promotion"]["apply_result"]["status"] == "applied"

    frozen = json.loads(
        (
            search_root
            / "specs"
            / linked["frozen_spec_id"]
            / "frozen_spec.json"
        ).read_text(encoding="utf-8")
    )
    assert frozen["spec"]["workspace"] == {
        "workspace_provider": "git_worktree",
        "promotion_mode": "apply",
    }
    assert frozen["spec"]["strategy"]["inner_agent"] == "common"
    assert frozen["spec"]["strategy"].get("worker_agent_type") is None

    candidates = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((run_dir / "candidates").glob("*/candidate.json"))
    ]
    assert len(candidates) == 2
    assert all(candidate["iterations"] for candidate in candidates)
    assert all(
        iteration["artifact_ref"]["kind"] == "git_commit"
        for candidate in candidates
        for iteration in candidate["iterations"]
    )
    assert all(
        candidate["task"]["strategy_metadata"]["worker_policy"][
            "inner_agent_binding"
        ]["workspace_ledger_projection"]
        == "none"
        for candidate in candidates
    )
    assert all(candidate["results_ledger"] for candidate in candidates)
    assert all(candidate.get("results_ledger_git_head") is None for candidate in candidates)
    assert all(
        iteration.get("ledger_git_head") is None
        for candidate in candidates
        for iteration in candidate["iterations"]
    )
    assert all(
        not (Path(candidate["task"]["workspace"]) / "results.tsv").exists()
        for candidate in candidates
    )

    sessions = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((run_dir / "agent_sessions").glob("agent_*.json"))
    ]
    assert len(sessions) == 2
    for session in sessions:
        launch = session["launch"]
        assert "普通 Search 候选 agent" in launch["prompt"]
        assert "worker_agent_type" not in launch
        metadata = session["session_handle"]["metadata"]
        assert metadata["runtime_start"]["active_non_builtin_tools"] == ["goal_plus_search_get_agent_context"]
        successful_tools = [
            entry["message"]["toolName"]
            for entry in _read_jsonl(Path(metadata["session_file"]))
            if entry.get("type") == "message"
            and entry["message"].get("role") == "toolResult"
            and entry["message"].get("isError") is False
        ]
        assert "goal_plus_search_run_verifier" in successful_tools
        assert successful_tools.index("goal_plus_search_get_agent_context") < successful_tools.index("goal_plus_search_run_verifier")
        assert session["counters"]["verifier_runs"] == metadata["dispatch_count"]

    workspace = example / "workspace"
    verified = subprocess.run(
        ["python", ".goal-plus-verifiers/circle_packing_score.py"],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=False,
    )
    assert verified.returncode == 0, verified.stderr or verified.stdout
    metrics = json.loads(verified.stdout.strip().splitlines()[-1])
    assert metrics["valid"] is True
    assert metrics["sum_radii"] == pytest.approx(run_record["selected_score"])
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=workspace,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert status == ""
    assert Path(linked["report_path"]).is_file()
    assert Path(linked["html_report_path"]).is_file()
    assert Path(linked["promotion_artifact_path"]).is_file()
    _assert_goal_plus_jsonl_is_clean(_session_file(session_dir))


@pytest.mark.st_pi
def test_goal_plus_print_supports_two_search_tasks_in_one_goal(
    st_pi_run_root: Path,
) -> None:
    search_root = st_pi_run_root / ".gp"
    session_dir = st_pi_run_root / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    command = [
        *_pi_base_command(session_dir, "st-pi-goal-plus-multi-search"),
        "-p",
        f"/goal-plus {MULTI_SEARCH_PROMPT}",
    ]

    result = subprocess.run(
        command,
        cwd=ROOT,
        env=_run_env(search_root),
        text=True,
        capture_output=True,
        timeout=int(os.environ.get("ST_PI_MULTI_SEARCH_TIMEOUT", "900")),
        check=False,
    )

    assert result.returncode == 0, result.stderr[-2000:] or result.stdout[-2000:]
    record = _goal_record(search_root)
    assert record["status"] == "complete"
    search_tasks = record.get("search_tasks") or []
    assert len(search_tasks) == 2, record
    run_ids = [task.get("run_id") for task in search_tasks]
    assert all(run_ids)
    assert len(set(run_ids)) == 2
    for task in search_tasks:
        assert task.get("selected_candidate_id"), task
        assert task.get("report_path"), task
        assert task.get("promotion_artifact_path"), task
        assert Path(task["report_path"]).exists()
        assert Path(task["promotion_artifact_path"]).exists()
        run_record = json.loads(
            (search_root / "runs" / task["run_id"] / "run.json").read_text(
                encoding="utf-8"
            )
        )
        assert run_record["state"] == "promoted"

    snapshot = goal_plus_monitor_snapshot(
        root_dir=search_root,
        goal_plus_id=record["goal_plus_id"],
    )
    assert snapshot["goal_plus"]["search_tasks_total"] == 2
    assert snapshot["search_task_aggregate"]["search_tasks_total"] == 2
    assert snapshot["search_task_aggregate"]["started_rounds_total"] == 2
    assert [task["strategy"]["name"] for task in snapshot["search_tasks"]] == [
        "random",
        "agent_guided",
    ]
    assert not any(
        warning["kind"] in {
            "linked_search_run_missing",
            "linked_search_spec_mismatch",
            "linked_search_frozen_spec_missing",
            "completed_goal_current_search_not_promoted",
        }
        for warning in snapshot["warnings"]
    )
    _assert_goal_plus_jsonl_is_clean(_session_file(session_dir))


@pytest.mark.st_pi
def test_goal_plus_print_revises_goal_and_frozen_spec_after_first_result(
    st_pi_run_root: Path,
) -> None:
    search_root = st_pi_run_root / ".gp"
    session_dir = st_pi_run_root / "sessions"
    workspace = st_pi_run_root / "spec-revision-workspace"
    session_dir.mkdir(parents=True, exist_ok=True)
    _write_spec_revision_workspace(workspace)
    command = [
        *_pi_base_command(session_dir, "st-pi-goal-plus-spec-revision"),
        "-p",
        f"/goal-plus {SPEC_REVISION_FIRST_PROMPT.format(workspace=workspace)}",
    ]

    first = subprocess.run(
        command,
        cwd=ROOT,
        env=_run_env(search_root),
        text=True,
        capture_output=True,
        timeout=int(os.environ.get("ST_PI_SPEC_REVISION_TIMEOUT", "900")),
        check=False,
    )

    assert first.returncode == 0, first.stderr[-3000:] or first.stdout[-3000:]
    first_record = _goal_record(search_root)
    assert first_record["status"] == "needs_user"
    assert first_record["goal_revision"] == 1

    result = subprocess.run(
        [
            *_pi_base_command(session_dir, "st-pi-goal-plus-spec-revision"),
            "-p",
            f"/goal-plus edit {SPEC_REVISION_SECOND_GOAL.format(workspace=workspace)}",
        ],
        cwd=ROOT,
        env=_run_env(search_root),
        text=True,
        capture_output=True,
        timeout=int(os.environ.get("ST_PI_SPEC_REVISION_TIMEOUT", "900")),
        check=False,
    )

    assert result.returncode == 0, result.stderr[-3000:] or result.stdout[-3000:]
    record = _goal_record(search_root)
    assert record["status"] == "complete"
    assert record["goal_revision"] == 2
    assert len(record["goal_revisions"]) == 2
    search_tasks = record.get("search_tasks") or []
    assert len(search_tasks) == 2, record
    assert [task["goal_revision"] for task in search_tasks] == [1, 2]
    frozen_spec_ids = [task["frozen_spec_id"] for task in search_tasks]
    run_ids = [task["run_id"] for task in search_tasks]
    assert len(set(frozen_spec_ids)) == 2
    assert len(set(run_ids)) == 2

    verifier_names = []
    for task in search_tasks:
        run_record = json.loads(
            (search_root / "runs" / task["run_id"] / "run.json").read_text(
                encoding="utf-8"
            )
        )
        assert run_record["state"] == "promoted"
        frozen_spec = json.loads(
            (
                search_root
                / "specs"
                / task["frozen_spec_id"]
                / "frozen_spec.json"
            ).read_text(encoding="utf-8")
        )
        verifier_names.append(
            Path(frozen_spec["spec"]["process_verifiers"][0]["command"][-1]).name
        )

    assert verifier_names == ["simple_score.py", "strict_score.py"]
    events = _goal_events(search_root, record["goal_plus_id"])
    assert [event["event_type"] for event in events].count("goal_updated") == 1
    assert [event["event_type"] for event in events].count("spec_draft_saved") >= 2
    _assert_goal_plus_jsonl_is_clean(_session_file(session_dir))


@pytest.mark.st_pi
@pytest.mark.skipif(
    os.environ.get("ST_PI_TUI") != "1" or shutil.which("tmux") is None,
    reason="set ST_PI_TUI=1 and install tmux to run the interactive native /goal-plus stats smoke",
)
def test_goal_plus_native_tui_stats_entry_does_not_trigger_followup(
    st_pi_run_root: Path,
) -> None:
    search_root = st_pi_run_root / ".gp"
    session_dir = st_pi_run_root / "sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_id = "st-pi-goal-plus-native"
    tmux_session = f"st-pi-{int(time.time())}"
    prompt = (
        "/goal-plus max_parallel=2 请只读检查 README.md 是否存在。不要修改文件。"
        "这个目标不需要 Search；max_parallel 配置可以保持未使用。"
        "最后把 Goal Plus 状态设为 complete。"
    )
    shell_command = shlex.join(
        [
            "env",
            f"GOAL_PLUS_ROOT={search_root}",
            f"GOAL_PLUS_SOURCE_PATH={ROOT}",
            *_pi_base_command(session_dir, session_id),
        ]
    )
    command = [
        "tmux",
        "new-session",
        "-d",
        "-s",
        tmux_session,
        "-x",
        "100",
        "-y",
        "30",
        shell_command,
    ]

    subprocess.run(command, cwd=ROOT, check=True)
    try:
        time.sleep(3)
        subprocess.run(["tmux", "send-keys", "-t", tmux_session, prompt, "Enter"], check=True)
        deadline = time.time() + int(os.environ.get("ST_PI_TIMEOUT", "420"))
        while time.time() < deadline:
            goal_files = sorted((search_root / "goal-plus").glob("gp_*/goal.json"))
            if goal_files:
                record = json.loads(goal_files[0].read_text(encoding="utf-8"))
                if record.get("status") == "complete":
                    break
            time.sleep(2)
        else:
            pane = subprocess.run(
                ["tmux", "capture-pane", "-t", tmux_session, "-p"],
                text=True,
                capture_output=True,
                check=False,
            )
            raise AssertionError(pane.stdout[-2000:])

        time.sleep(8)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", tmux_session], check=False)

    session_path = _session_file(session_dir)
    _assert_goal_plus_jsonl_is_clean(session_path)
    entries = _read_jsonl(session_path)
    record = _goal_record(search_root)
    assert record["command_config"] == {"max_parallel": 2}
    assert record["spec_draft"] is None
    assert record["search_tasks"] == []
    session_text = session_path.read_text(encoding="utf-8")
    assert "GoalPlusEntrypointRequired" not in session_text
    command_contexts = [
        entry
        for entry in entries
        if entry.get("type") == "custom_message"
        and entry.get("customType") == "goal-plus-command-context"
    ]
    assert len(command_contexts) == 1
    assert command_contexts[0].get("display") is False
    assert not any(
        (entry.get("message") or {}).get("role") == "user"
        and "继续此 Goal Plus 任务" in json.dumps(
            (entry.get("message") or {}).get("content"), ensure_ascii=False
        )
        for entry in entries
    )
    stats_indexes = [
        index
        for index, entry in enumerate(entries)
        if entry.get("type") == "custom"
        and entry.get("customType") == "goal-plus-stats"
    ]
    assert stats_indexes
    assert not any(
        (entry.get("message") or {}).get("role") == "assistant"
        for entry in entries[stats_indexes[-1] + 1 :]
    )
