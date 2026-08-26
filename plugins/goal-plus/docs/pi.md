# Pi

Pi 以可安装 package 的形式提供一个面向用户的 `goal-plus` skill 和一个 extension，底层复用共享 Goal Plus runtime。它不修改 Pi core，也不暴露独立的用户级 `search` skill；开发 checkout 可以直接安装同一组 asset。

## 安装与快速开始

### 用户安装

在当前 Muyuan checkout 的 Goal Plus source directory 中安装 runtime 和 Pi package：

```bash
cd <Muyuan checkout>/plugins/goal-plus
python -m pip install .
pi install .
```

Python 环境可以自行选择，但其中的 Goal Plus executable 必须出现在 Pi 继承的 `PATH` 中；如果目标环境已经安装正确版本，可以省略 pip 命令。Pi 会直接加载这个 local package，因此安装期间需要保留 source directory。

移除 Pi 集成：

```bash
pi remove .
```

只有在其他 Pi 或 Codex 安装都不再使用该 Python 环境中的 Goal Plus 命令时，才运行 `python -m pip uninstall -y goal-plus`。

### Checkout 开发安装

```bash
cd <Muyuan checkout>/plugins/goal-plus
python -m pip install -e ".[dev]"
pi install .
pi -p "/goal-plus inspect this repository"
```

通过 `pi remove .` 移除 checkout package。用户安装和开发安装暴露相同的 extension，以及同一个面向用户的 `goal-plus` skill。

#### Progress UI 开发预览

extension 可以在不创建 Goal Plus record、Search run、candidate workspace 或模型请求的情况下显示 fixture-backed Search progress。这只是开发预览，不是 benchmark Evidence：

```bash
GOAL_PLUS_UI_PREVIEW=active \
  pi --offline --no-session --no-extensions \
  -e .pi/extensions/goal-plus.ts
```

在该 session 中使用 `/goal-plus-ui-preview active`、`/goal-plus-ui-preview complete`、`/goal-plus-ui-preview result` 或 `/goal-plus-ui-preview clear` 切换状态。`result` 会用 Circle Packing 的真实样例数值追加一张终态完成卡，不需要运行模型、verifier 或完整 Goal Plus 流程；普通 session 不注册该命令。体验目标和字段语义见 [Lightweight Search Progress UX](usability-progress.md)。

也可以在交互式 Pi 中直接使用：

```text
/goal-plus Fix this bug and verify the test suite.
/goal-plus mode=probe Check whether vectorization is viable.
/goal-plus mode=autonomous Deeply optimize the kernel.
```

`/goal-plus edit`、`/goal-plus resume` 和 `/goal-plus-with-final-check` 与 Codex 使用同一套 goal revision 语义。required final check 由独立、只读的 Pi RPC reviewer 执行。

## Pi 与 Codex 的差异

Pi 使用 extension event，而不是项目 hook 文件。extension 会在 interactive、RPC、print 和 JSON 模式下，在模型运行前创建 `/goal-plus`，保存 active ID，注入隐藏 context，并对 write/Search tool 执行 gate。它提供原生 turn-level stop gate，但不是 host process 级 Stop hook。

新建 Goal Plus 记录前，Pi extension 会追加一条不进入模型上下文的 `goal-plus-runtime-start` custom entry。折叠视图显示当前 main session 的 mode、project trust、active tool 数量，以及 Pi 没有内置 sandbox/审批的事实；展开后显示实际 cwd 和 active builtin/non-builtin tool 名单。这里的 `--approve` 或 `project_trusted=true` 只表示允许加载项目内 Pi 资源，不表示 full access。filesystem 只报告为 `launcher-process-user`，network 只报告为 `host-policy-unreported`，不会从 settings 或父进程猜测不存在的 Pi permission profile。

Goal Plus 启动的 ordinary、final-checker 和 Search candidate `pi-rpc` 子进程会在各自 `session_start` 写入同一结构。wrapper 在发送第一条模型 prompt 前从该 native session 读回快照，写入 host event log 的 `goal_plus_runtime_start` 事件、handle `metadata.runtime_start` 和 `pi_metrics.runtime_start`。`search_get_agent_observability` 将其投影为 `runtime_start`；快照缺失时保留 `source="unavailable"` 和错误原因，不从 main session 继承或推断。headless worker 不弹出不可见的 TUI 通知，运行中排障可直接查看对应 `.gp/host-logs/pi-rpc-<agent_session_id>.jsonl`。

Pi Search 的异步 Evidence annotation 也运行在 Pi 上。run-scoped drainer 启动临时 `pi --mode json --no-session --no-tools` 进程，默认继承 Pi worker 的 model/provider；显式 qualified annotator model 或 `evidence_annotator.pi_provider` 可以单独选择 provider。verifier settlement 不等待该进程，同一次 annotator turn 也会生成 shared-dir Tool View。完整生命周期见[共享平面](shared-plane.md#工具复制与采用结算)。

main turn 结束时，仍为 active 的 Goal Plus record 会携带完整 raw goal 和 elapsed-time context 继续执行。Pi 只有在 agent 写入终态后才停止；worker watchdog 到期只是一次 dispatch event，不代表 goal 完成。

进入终态时，Pi extension 会在 `agent_end` 后静默通过只读 monitor 读取持久化结果，再写入唯一一条可展开的 Goal Plus 完成卡。模型不需要为了最终展示再次调用 monitor；最终回复只保留一句交接语，避免自然语言结果、可见 tool JSON 和完成卡三重重复。默认视图只保留四类高价值信息：终态与整个目标耗时、选中结果及相对基线变化、candidate/verifier/promotion 摘要，以及 main+worker 合计用量。整个目标明确给出时间窗口时，卡片会同时显示期限和超出量。

完成卡行间不加入额外纵向 padding。展开完成卡可以查看 main/worker 用量拆分、每个 candidate 的模型/分数/verifier/disposition、Goal Plus ID 和报告路径。若模型因过程判断主动调用 `goal_plus_monitor_snapshot`，Pi 的自定义 tool renderer 默认只显示终态、选中结果、verifier 和 promotion 的单行摘要；展开也只增加 run/candidate/phase 摘要，完整 JSON 仍提供给模型但不铺满 TUI。monitor 不可用时，卡片降级到终态、当前会话耗时和 main 用量；不会从 transcript 猜测缺失数据。它是 custom entry，不是 LLM message，不会触发另一个 assistant turn，也不再额外发送一份重复的统计通知。

Search worker 是由 host-local durable pool 监督的 Pi RPC 进程。每个 wrapper 通过 `goal-plus-pi-worker` 启动前台 `pi --mode rpc` 子进程；native session 位于 `.gp/host-sessions/pi/`，pool 状态位于 `.gp/host-pools/pi/`。进程可以重启，但 candidate workspace 和 native session 提供连续性。

## 普通 Goal Mode

普通任务使用 [`goal-plus-ultra-v1`](ultra.md) work DAG。逻辑上的 Main `thinking=max` 会映射为 Pi 原生 `xhigh` 请求，再记录模型 clamp 后的实际 level；如果结果为 `off`，该 Ultra binding 会拒绝继续。

`route="subagent"` 的 work item 通过 `pi_goal_plus_run_work_item` 执行。该工具启动隔离的 Pi RPC child，在进程启动后绑定 durable attempt，等待 child 完成，并用同一个 `attempt_id` 和 `generation` 提交 result。已经 active 的 item 会被拒绝，不会悄悄创建 replacement session。多个相互独立的 tool call 可以并发执行。

Pi wrapper 只拥有 native launch/wait。main 仍负责 acceptance、rework、integration、Search routing 和 completion。完整 work-item state machine 见 [Ultra Orchestration](ultra.md)。

## Search Pool 流程

Pi Search 使用 `strategy.orchestration_mode="parallel_loops"` 和固定的初始 lane：

1. 只规划并物化一次初始 candidate；
2. 调用 `pi_search_pool_open(..., max_parallel=<frozen limit>)`；
3. 用 `pi_search_pool_wait_any` 等待任意终态 event；
4. 收到 `candidate_ready` 后结算 verifier-backed best；若全局停止条件尚未满足，对同一 candidate 调用 `pi_search_pool_continue`；
5. main turn 中断后，用 `pi_search_pool_snapshot(run_id=...)` 重新发现 pool；
6. 结束时调用 `pi_search_pool_close`，再 select 和 promote；
7. 记录 Search result，完成 raw-goal audit，设置 Goal Plus 终态，并为每个已记录 run 生成一次 final report。

supervisor 只维护初始固定 candidate，不提供 submit/refill，也不会因为低分或没有提升替换 candidate。`candidate_ready` 必须同时满足 minimum lease 已释放、handle 已绑定且 durable Evidence 已落盘；`timed_out`、`interrupted` 和 `failed` 都不能作为 ready candidate 继续。

driver 复用匹配的 worker Evidence，不创建重复的 parent process iteration。只有 workspace 已改变但缺少 worker Evidence 时，parent 才补做 final verifier；未改变的 workspace 不会增加 baseline iteration。

## 模型与 Worker Spec

Pi 使用 `worker_host="pi-rpc"` 和 wall-clock budget：

```json
{
  "strategy": {
    "name": "random",
    "orchestration_mode": "parallel_loops",
    "worker_host": "pi-rpc",
    "worker_budget": {
      "min_runtime_seconds": 500,
      "min_verifier_runs": 1,
      "max_runtime_seconds": 600,
      "max_turns": 8,
      "on_exceed": "interrupt"
    }
  }
}
```

`max_runtime_seconds` 必填。`max_turns` 只是 prompt 提示。可选 `min_runtime_seconds` 和 `min_verifier_runs` 由 pool wrapper 累计执行：Pi turn 提前结束时，会在同一 slot/worktree 恢复相同 native session，并只获得剩余上限预算。达到 hard deadline 前，runner 发送一次 closeout steer；基础设施故障、pool close 或外层 closeout 会停止自动续跑。

命令可分别选择 Evidence annotator 和 Search worker 模型：

```text
/goal-plus annotator=B workers=C,D max_parallel=4 optimize ...
```

已经运行的 Pi session 就是 main agent，`/goal-plus` 不能切换它的模型。应在启动时使用 `pi --model MODEL`，或在提交 Goal Plus prompt 前使用 `/model`。正常使用应省略 `main=`；为兼容旧命令，`main=` 只作为当前 session model 的可选 assertion：extension 会校验并在不匹配时阻止执行，但不会把它当成 selector。

`annotator=` 冻结 `strategy.evidence_annotator.model`；`workers=` 将一个或多个模型解析到 `strategy.models`。省略某个 role 时保留 host default 或正常继承，不从其他显式 role 推导。

多个 provider 暴露同一模型 ID 时，使用 `provider/model`：

```text
/goal-plus workers=openai-codex/gpt-5.6-terra,openai-codex/gpt-5.6-sol max_parallel=4 optimize ...
/goal-plus workers=openai-codex/gpt-5.6-terra,openai-codex/gpt-5.6-sol A1B3 max_parallel=4 optimize ...
```

Goal Plus 通过 `goal_plus_list_models(host="pi-rpc")` 调用 Pi RPC `get_available_models`。短 ID 只有在唯一匹配时才会接受。没有 count 的条目按 round-robin 分配；`A1B3` 或 `A*1,B*3` 会生成显式 count，且总数必须等于 `max_parallel`。`models=` 是 `workers=` 的兼容别名，两者不能同时出现。最终 `selected_models` 对每个 candidate/native session 不可变；同时省略 `workers=` 和 `models=` 时使用 Pi 当前 default。

## Worker 上下文与恢复

candidate worker 只能使用 `search_get_agent_context`、`search_get_global_evidence`、`search_get_evidence_detail`、`search_reference_open`、`search_stage_shared_tool`、`search_copy_shared_tool`、`search_run_verifier` 和 `search_list_iterations`。`search_reference_open` 返回当前 worker 唯一的 reference checkout 路径；worker 直接用普通文件和命令工具查看，再次调用时同一目录切到新的 Evidence commit。

`search_get_agent_context` 返回有界的 best/recent 摘要、权威 ID、workspace 和 durable `results.tsv` 路径。只有确实需要旧 verifier 的完整细节时，才调用可能很大的 `search_list_iterations`。

worker 始终把 `context.workspace` 作为文件与 shell 操作的显式 cwd，不依赖 wrapper 启动
目录。任务局部 Skill 无需 spec 配置：runtime 只发现 source Git 已跟踪的
`.agents/skills/<name>/SKILL.md`，并要求该 Skill 目录内所有文件都由 Git 跟踪；在 freeze 的
`task_skill_notice` 和 launch prompt 中明确列出，
并通过 `candidate_task.task_skill_paths` 返回当前 worktree 的绝对路径。worker 完整读取这份
清单，不扫描 parent/global/plugin 或其他 candidate 中的 Skill。

manual 模式下，worker 在第一次修改前读取 Global Evidence，之后按配置周期或停滞节点刷新；auto 模式使用 verifier 自动注入的共享快照，independent 模式不提供 peer Evidence。worker 独立选择方向，交付修改限于 `allowed_files`，并可在 `.tmp/` 保存 scratch 和更新 `handoff.json`。每项实质变更用一句话 `hypothesis` 调用 verifier；`null` View 只表示 annotator 尚未发布，不要求等待。

同进程 continuation 沿用已经加载的 context 和 Evidence。跨进程恢复同一 native session 时，只重新加载紧凑的权威 candidate context，完整 worker contract 和已加载 Evidence 仍由持久 session 保留。

如果 assistant response 以 `stopReason="length"` 结束且没有 tool call，worker-local extension 不再续接该上下文。driver 会返回 refresh signal，由 pool supervisor 为同一 candidate 和 workspace 创建 fresh native session 与新的 `agent_session_id`；Git 修改和 durable Evidence 继续保留。

无法恢复 native session 时，handoff、candidate Git state 和 `.gp` verifier history 是持久恢复依据。main 只发送中性 continuation directive，不替 worker 选择技术方向。

## 工具、Observability 与日志

`goal-plus-pi-tool` 是 Pi 本地 JSON CLI facade，与 MCP 暴露相同的 GoalPlusTools/SearchTools，并提供 host-local pool 操作。只读 monitor 示例：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

`goal_plus_monitor_snapshot` 不启动、等待或停止 worker。`search_get_agent_observability(agent_session_id)` 返回与 Codex 相同的归一化 schema；Pi 将 `pi_metrics` 中的 model、thinking level、duration、usage/cost、context 和 log/session path 投影到该结构。当已产生 token 但 Pi 因缺少单价而返回零费用时，归一化结果将 cost 保留为未知，不影响运行状态。

普通 Ultra work 可以启用 `orchestration` feature plugin。Pi wrapper 会把 `pi_goal_plus_run_work_item` 和 `goal-plus-pi-worker` 保存为 namespaced event metadata；plugin 将其投影成与 Codex 相同的 task/result/main-decision 结构，但不会改变 Pi RPC process ownership。详见 [Monitor Feature Plugins](feature-plugins.md)。

Search state、candidate commit 和 workspace 以 `.gp/` 为准。默认 worker log 是只含元数据的 event log：

```text
.gp/host-logs/pi-rpc-<agent_session_id>.jsonl
```

它记录 event/tool status、有界 error、timing 和 usage，不含 prompt 或 reasoning。只有短时聚焦调试确实需要完整 RPC payload 时才设置 `GOAL_PLUS_PI_RAW_LOG=1`；raw stream 可能很大。

已绑定 handle 中的 `metadata.pi_metrics` 保存 model、thinking level、timeout/failure Evidence 和有界 `metadata.progress_handoff`。timeout 表示 deadline enforcement 生效；runner failure 会绑定 synthetic failure metadata，避免 monitor 把 session 误判为仍然存活。

完整 API 见 [API](api.md)，跨 host 诊断见[调试运行时状态](debugging-runtime.md)。

## 支持范围与验证

Pi 当前只支持以下可移植内置策略：

- `agent_guided`、`agent`、`default`
- `random`、`random_mode`

快速测试：

```bash
pytest -m pi -q
```

真实 host smoke test：

```bash
ST_PI_CYCLE_WORKER_SECONDS=120 \
  pytest -m "st and st_pi_rpc" -k managed_pool_wait_any -v -s -rs
```

该场景启动两个独立 Pi RPC worker，通过 `run_id` 重新发现 pool，观察 wait-any completion，并正常 drain。
