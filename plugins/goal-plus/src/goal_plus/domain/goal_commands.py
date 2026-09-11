"""Typed Goal command configuration and deterministic dispatch constants."""

from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Literal
from goal_plus.domain.goal import GoalPlusCommandConfig, GoalPlusExecutionPolicy
from goal_plus.domain.worker_policy import parse_worker_model_counts
from goal_plus.domain.goal_authorization import SEARCH_RECOVERY_TOOL_SUFFIXES

GOAL_PLUS_EDIT_ENTRYPOINT_REQUIRED = (
    "GoalPlusEntrypointRequired: Goal Plus objectives can only be edited by "
    "Codex $goal-plus edit or Pi /goal-plus edit. MCP, CLI, skill, and "
    "model-initiated edits are not permitted."
)


DEFAULT_EXECUTION_POLICY = GoalPlusExecutionPolicy().model_dump(mode="json")


DEFAULT_WORK_LAUNCH_TTL_SECONDS = 120


WORK_EVENT_STATUS = {
    "bind": "active",
    "result": "result_ready",
    "accepted": "accepted",
    "failed": "failed",
    "cancelled": "cancelled",
}


WORK_EVENT_ALLOWED_STATUSES = {
    "dispatch": {"launching", "result_ready", "accepted", "failed", "cancelled"},
    "bind": {"launching"},
    "message": {"active"},
    "result": {"active"},
    "rework": {"result_ready"},
    "accepted": {"result_ready"},
    "failed": {"launching", "active"},
    "cancelled": {"launching", "active", "result_ready", "failed"},
}
WORK_EVENT_SUMMARY_MAX_CHARACTERS = 4000


EXPLORATION_MODES = {"autonomous", "probe"}


EXPLORATION_MODE_LINE_PREFIX = "Goal Plus 探索模式："


LEGACY_EXPLORATION_MODE_LINE_PREFIX = "Goal Plus exploration mode:"


EXPLORATION_MODE_LINES = {
    "autonomous": (
        "Goal Plus 探索模式：autonomous。候选返回后，Main 根据目标完成情况、持久证据和"
        "用户明确给定的时间上限判断是否继续；需要继续时复用同一候选和 session，让候选"
        "自行选择下一个有证据支持的方向。分数或排名不能导致替换，单次调用返回或等待"
        "超时不代表 Goal Plus 任务完成。用户要求停止时停止。"
    ),
    "probe": (
        "Goal Plus 探索模式：probe。围绕可行性、潜力和关键阻塞因素安排有界尝试；候选"
        "返回后由 Main 根据持久证据判断是否需要继续，并尊重用户明确给定的时间上限。"
        "需要继续时复用同一候选和 session，让候选自行选择下一个有证据支持的步骤。"
        "分数或排名不能导致替换，单次 probe 返回不代表 Goal Plus 任务完成。用户要求停止时停止。"
    ),
}


@dataclass(frozen=True)
class GoalPlusCommandOption:
    """Public metadata for one canonical host-command configuration option."""

    key: str
    model_field: str
    value_syntax: str
    default_description: str
    description: str
    aliases: tuple[str, ...] = ()
    value_kind: Literal[
        "raw", "choice", "positive_integer", "single_model", "worker_models"
    ] = "raw"
    choices: tuple[str, ...] = ()

    @property
    def accepted_keys(self) -> tuple[str, ...]:
        return (self.key, *self.aliases)


GOAL_PLUS_COMMAND_OPTIONS = (
    GoalPlusCommandOption(
        "mode",
        "mode",
        "autonomous | probe",
        "`autonomous`",
        "探索模式；省略时默认为 `autonomous`。",
        value_kind="choice",
        choices=("autonomous", "probe"),
    ),
    GoalPlusCommandOption(
        "max_parallel",
        "max_parallel",
        "正整数",
        "未设置；由 Agent 根据任务和资源决定。",
        "约束 `SearchSpec.budget.max_parallel`。",
        value_kind="positive_integer",
    ),
    GoalPlusCommandOption(
        "max_duration_seconds",
        "max_duration_seconds",
        "正整数（秒）",
        "未设置；不创建命令级外层 deadline。",
        "从 Goal 创建时间起固定外层 deadline，覆盖 worker、verifier 和收尾边界。",
        value_kind="positive_integer",
    ),
    GoalPlusCommandOption(
        "workspace_provider",
        "workspace_provider",
        "copy | git_worktree | thinkthread",
        "未设置；由 Agent 根据任务和 host 能力决定。",
        "约束 `SearchSpec.workspace.provider`。",
        value_kind="choice",
        choices=("copy", "git_worktree", "thinkthread"),
    ),
    GoalPlusCommandOption(
        "promotion_mode",
        "promotion_mode",
        "apply | artifact_only",
        "未设置；由 Agent 根据任务授权和 workspace 决定。",
        "约束 `SearchSpec.workspace.promotion_mode`。",
        value_kind="choice",
        choices=("apply", "artifact_only"),
    ),
    GoalPlusCommandOption(
        "strategy",
        "strategy",
        "NAME",
        "未设置；由 Agent 选择 maintained strategy。",
        "约束 `SearchSpec.strategy.name`。",
    ),
    GoalPlusCommandOption(
        "inner_agent",
        "inner_agent",
        "ADAPTER_ID",
        "未设置；direct 默认 autoresearch，ThinkThread 默认 common。",
        "约束 `SearchSpec.strategy.inner_agent`。",
    ),
    GoalPlusCommandOption(
        "annotator",
        "annotator",
        "MODEL",
        "未设置；不单独指定 annotator model。",
        "约束 `SearchSpec.strategy.evidence_annotator.model`。",
        value_kind="single_model",
    ),
    GoalPlusCommandOption(
        "workers",
        "workers",
        "MODEL[,MODEL...] 或 MODEL*COUNT[,MODEL*COUNT...]",
        "未设置；worker 使用 host 的 parent/default model。",
        "约束 `SearchSpec.strategy.models`；带 count 时每个模型都必须带 count。",
        aliases=("models",),
        value_kind="worker_models",
    ),
)


GOAL_PLUS_COMMAND_OPTION_BY_KEY = {
    key: option for option in GOAL_PLUS_COMMAND_OPTIONS for key in option.accepted_keys
}


GOAL_PLUS_COMMAND_CONFIG_KEYS = frozenset(GOAL_PLUS_COMMAND_OPTION_BY_KEY)


def render_goal_plus_command_config_markdown() -> str:
    """Render the public command configuration table from the runtime registry."""

    lines = [
        "| Key | Value | Default（省略时） | 约束目标 |",
        "| --- | --- | --- | --- |",
    ]
    for option in GOAL_PLUS_COMMAND_OPTIONS:
        keys = " / ".join(f"`{key}`" for key in option.accepted_keys)
        value_syntax = option.value_syntax.replace("|", r"\|")
        lines.append(
            f"| {keys} | `{value_syntax}` | {option.default_description} | "
            f"{option.description} |"
        )
    return "\n".join(lines)


def render_goal_plus_command_usage() -> str:
    """Render the compact config portion of host command usage text."""

    options = []
    for option in GOAL_PLUS_COMMAND_OPTIONS:
        key = option.key
        if option.aliases:
            key = f"{key}|{'|'.join(option.aliases)}"
        options.append(f"[{key}={option.value_syntax.replace(' | ', '|')}]")
    return " ".join(options)


def _parse_command_option_value(
    option: GoalPlusCommandOption, raw_value: str
) -> str | int:
    if option.value_kind == "positive_integer":
        try:
            return int(raw_value)
        except ValueError as exc:
            raise ValueError(f"{option.key}= requires a positive integer") from exc
    if option.value_kind == "choice":
        value = raw_value.lower()
        if value not in option.choices:
            allowed = ", ".join(option.choices)
            option_name = "exploration mode" if option.key == "mode" else option.key
            raise ValueError(
                f"unsupported Goal Plus {option_name} {value!r}; "
                f"expected one of: {allowed}"
            )
        return value
    if option.value_kind == "single_model" and "," in raw_value:
        raise ValueError(
            f"{option.key}= accepts exactly one model; use workers= for a list"
        )
    if option.value_kind == "worker_models":
        if any(not value.strip() for value in raw_value.split(",")):
            raise ValueError("workers=/models= contains an empty model reference")
        parse_worker_model_counts(raw_value)
    return raw_value


_COMMAND_CONFIG_DIRECTIVE_RE = re.compile(
    r"^(?P<name>[A-Za-z][A-Za-z0-9_-]*)=(?P<value>[^\s]+)(?:\s+|$)"
)


SEARCH_TOOL_SUFFIXES = (
    "goal_plus_search_freeze_spec",
    "goal_plus_search_create",
    "goal_plus_search_status",
    *SEARCH_RECOVERY_TOOL_SUFFIXES,
    "goal_plus_search_list_history",
    "goal_plus_search_plan_next",
    "goal_plus_search_start_batch",
    "search_start_agent_session",
    "search_redispatch_candidate",
    "search_bind_agent_handle",
    "search_continue_agent_session",
    "goal_plus_search_run_verifier",
    "goal_plus_search_wait_verifier",
    "goal_plus_search_select",
    "goal_plus_search_report",
    "goal_plus_search_promote",
    "goal_plus_search_apply_promotion",
)


MUTATING_TOOL_SUFFIXES = (
    "bash",
    "edit",
    "write",
    "shell",
    "exec_command",
    "apply_patch",
)


def exploration_mode_from_raw_goal(raw_goal: str) -> str | None:
    lines = raw_goal.rstrip().splitlines()
    if not lines:
        return None
    final_line = lines[-1].strip()
    for mode in EXPLORATION_MODES:
        if final_line == EXPLORATION_MODE_LINES[mode] or final_line.startswith(
            f"{LEGACY_EXPLORATION_MODE_LINE_PREFIX} {mode}."
        ):
            return mode
    return None


def parse_goal_plus_command_config(
    raw_goal: str,
) -> tuple[str, GoalPlusCommandConfig]:
    """Split leading typed key=value directives from the natural-language goal."""

    remaining = raw_goal.strip()
    directives: dict[str, str] = {}
    while remaining:
        match = _COMMAND_CONFIG_DIRECTIVE_RE.match(remaining)
        if match is None:
            break
        name = match.group("name")
        value = match.group("value").strip()
        if name == "main":
            raise ValueError(
                "main= is not supported; select the main model when starting "
                "Codex/Pi or with /model before /goal-plus"
            )
        if name == "model":
            raise ValueError("model= is not supported; use annotator= and workers=")
        if name not in GOAL_PLUS_COMMAND_CONFIG_KEYS:
            supported = ", ".join(sorted(GOAL_PLUS_COMMAND_CONFIG_KEYS))
            raise ValueError(
                f"unsupported Goal Plus command config {name}=; supported: {supported}"
            )
        if name in directives:
            raise ValueError(f"/goal-plus accepts at most one {name}= directive")
        directives[name] = value
        remaining = remaining[match.end() :].lstrip()

    parsed: dict[str, str | int] = {}
    worker_directive: str | None = None
    for option in GOAL_PLUS_COMMAND_OPTIONS:
        supplied_keys = [key for key in option.accepted_keys if key in directives]
        if len(supplied_keys) > 1:
            aliases = " and ".join(f"{key}=" for key in supplied_keys)
            raise ValueError(f"{aliases} are aliases; specify only one")
        if not supplied_keys:
            continue
        supplied_key = supplied_keys[0]
        parsed[option.model_field] = _parse_command_option_value(
            option, directives[supplied_key]
        )
        if option.model_field == "workers":
            worker_directive = supplied_key

    config = GoalPlusCommandConfig.model_validate(
        {
            **parsed,
            "worker_directive": worker_directive,
        }
    )
    if not remaining:
        raise ValueError("raw_goal must include an objective after command config")
    return remaining, config


def normalize_goal_plus_raw_goal(
    raw_goal: str,
    *,
    inherited_mode: str | None = None,
) -> str:
    normalized, command_config = parse_goal_plus_command_config(raw_goal)
    explicit_mode = command_config.mode

    embedded_mode = exploration_mode_from_raw_goal(normalized)
    if embedded_mode is not None:
        normalized = normalized.rsplit("\n", 1)[0].rstrip()

    if not normalized:
        raise ValueError(
            "raw_goal must include an objective after the exploration mode"
        )

    mode = explicit_mode or embedded_mode or inherited_mode or "autonomous"
    if mode not in EXPLORATION_MODES:
        mode = "autonomous"
    return f"{normalized}\n\n{EXPLORATION_MODE_LINES[mode]}"
