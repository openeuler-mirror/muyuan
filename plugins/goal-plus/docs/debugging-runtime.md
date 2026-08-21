# 调试运行时状态

本文用于检查正在运行或已经结束的 Goal Plus（Codex 使用 `$goal-plus`，Pi 使用 `/goal-plus`）：main 和 worker 处于什么状态、Search 产生了哪些 verifier Evidence，以及故障发生在 runtime 还是 host。

## 推荐排查顺序

1. 先读取 `goal_plus_monitor_snapshot`，确定 goal revision、work item、Search run、candidate 和 warning；
2. monitor 缺少所需细节时，再检查 `.gp/` 中的持久状态和 verifier log；
3. 只有需要还原 worker 行为或 host lifecycle 时，才读取 Codex/Pi 原生日志；
4. 始终通过 `goal_plus_id`、`run_id`、`candidate_id` 和 `agent_session_id` 交叉关联，不能从 prose 猜测缺失事实。

## Monitor Snapshot

`goal_plus_monitor_snapshot` 是首选的只读入口。它不会启动、等待、继续或停止 worker。MCP 不便直接调用时，可以使用 Pi facade：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"goal_plus_id":"gp_...","stale_after_seconds":600}' \
  --pretty
```

只知道 `run_id` 时也可以直接查询该 run。需要关闭可选投影时传入 `feature_plugins=[]`；默认的 `orchestration` plugin 会显示普通 subagent work 的 task packet、不透明 worker handle、host 原生操作、attempt generation、`launching`/`bound` 状态、stale-result 数量，以及 assignment/result/decision 流程。它不包含 transcript 路径或内容。

### Goal 与 Work DAG

首先检查：

- `goal_revision`、`goal_revisions_total`、`phase` 和 `next_action`；
- `work_items` 中 required item 是否已 `accepted`；
- subagent attempt 的 `attempt_id`、`generation`、launch deadline 和 bound handle；
- `feature_plugins.orchestration` 中是否存在迟到的 `stale_result`，以及 main 是否已经记录 acceptance/rework decision；
- `final_check_policy` 和 `latest_final_check` 是否对应当前 revision。

`dispatch` 后长期停留在 `launching` 通常表示 host 尚未成功 `bind`。新 generation 已经派发后，旧 generation 的 result 只会成为 `stale_result` Evidence，不会推进当前 work item。

### Search Run

一条 goal 可以追加多个 Search task。`search_tasks[]` 是完整历史，`linked_search` 和顶层详细 `run` 只是当前 task 的兼容视图。不要用 `linked_search != null` 推断 task 数量。

重点字段包括：

- run state、frozen spec、strategy 和唯一初始 plan；
- candidate score、iteration、session、verifier count 和 current best；
- worker duration、usage、context、artifact 和 stale/timed-out warning；
- selection、promotion 和 report path；
- `statistics.selected_run` 中的 baseline/target improvement、time-to-first-verifier/success、worker outcome、model/provider、selection survival、usage、efficiency 和 `unavailable_metrics`。

`search_task_aggregate` 汇总全部 task 的 plan、candidate、session、verifier 和已知 cost。`statistics.orchestrator` 只包含归一化 Codex usage 与 identity metadata；`statistics.total_usage` 将其与 worker usage 合并，并保留覆盖率，避免把已知小计误当成完整 cost。

每个 `subagents[]` 条目都包含版本化 `observability`。`subagents[].verifier_count` 和 `session_verifier_count` 属于当前 session，`candidate_verifier_count` 是 candidate 累计值。需要单独查询时使用 `search_get_agent_observability(agent_session_id)`。

### Terminal Dashboard 与 Report

需要持续查看项目目录时使用只读 dashboard：

```bash
./scripts/monitor_goal_plus.sh /path/to/project
./scripts/monitor_goal_plus.sh --once --goal gp_... /path/to/project
./scripts/monitor_goal_plus.sh --run run_... --no-clear /path/to/project
INTERVAL=2 RUN_LIMIT=0 ./scripts/monitor_goal_plus.sh /path/to/project
```

默认输出是一屏摘要；`--verbose` 展开 worker usage、identity、directive、handoff、advisory、artifact 和 orchestration event summary，`--json` 输出完整组装 payload。Pi pool 部分只读取持久 snapshot，不执行 reconciliation、wait、close 或 interrupt。

`search_report(run_id)` 从同一份持久 Evidence 生成 `report.md` 和 self-contained `report.html`。关联 Goal Plus 的 run 只有在 record 已进入 `complete`、`blocked` 或 `abandoned` 后才能生成报告；正常顺序是 select、promote、record Search result、final audit、terminal status，最后为每个已记录 run 生成一次 report。

HTML 是生成时刻的静态视图。后续产生新的 hook event 或 observability 后，需要再次调用 `search_report` 才会更新。缺失数据保持 `Not observed` 或列入 `unavailable_metrics`，不会从 transcript prose 补造。

## Runtime 与 Host 的边界

Goal Plus 有两层状态：

| Owner | 负责内容 |
|---|---|
| runtime | goal/work DAG、frozen spec、candidate workspace、iteration、verifier score、selection、report 和 promotion patch |
| Codex/Pi/其他 host | worker launch、wait、message、continuation、deadline、interrupt 和原生日志 |

MCP runtime 不维护 worker process lifecycle、host-sync state 或 cancellation。内部 Evidence annotator 是例外：candidate 下保存 annotation task 和有界 retry，run 下保存 single-flight drainer state。

因此，`.gp/` 说明“runtime 接受了什么事实”，host log 说明“worker 和 supervisor 实际做了什么”。`AgentSessionRecord` 是 context/provenance handle，不是 liveness record。

## `.gp/` 持久状态

```text
.gp/
├── goal-plus/<goal_plus_id>/
│   ├── goal.json                                # 当前 goal、revision、work item 和 Search link
│   └── events.jsonl                             # append-only transition Evidence
├── specs/<frozen_spec_id>/
│   ├── frozen_spec.json                         # frozen SearchSpec
│   └── verifier_artifacts/<path>                # hash-pinned verifier 文件
└── runs/<run_id>/
    ├── run.json                                 # RunRecord 和 current best
    ├── best.json                                # best candidate/iteration/commit 指针
    ├── plans/<plan_id>.json                     # 初始 candidate 分配
    ├── candidates/<candidate_id>/
    │   ├── candidate.json                       # iterations[]、results_ledger[] 和 score
    │   ├── task.json                            # CandidateTask snapshot
    │   ├── evidence-annotations/iteration-<n>.json
    │   └── logs/iteration-<n>-<verifier>-<id>.log
    ├── workspace/<candidate_id>/
    │   ├── .git/
    │   ├── results.tsv
    │   └── <allowed_files>
    ├── shared/index.json                        # shared-dir family/tool index
    ├── shared/tools/<candidate_id>/<iteration>/ # immutable tool snapshot
    ├── agent_sessions/<agent_session_id>.json
    ├── report.md / report.html
    └── promotion/
```

新的 `parallel_loops` run 在 `plans/` 下只有一个初始 plan，但同一 candidate 可以有多个 session 和 iteration。旧 run 可能包含多个 plan。

### Candidate 与 Verifier Evidence

快速查看最新 run：

```bash
RUN=$(ls -td .gp/runs/* | head -1)
python3 -c "
import json
d = json.load(open('$RUN/run.json'))
print(f\"state={d['state']} candidates={d['candidates_total']}/{d.get('candidates_evaluated', 0)} best={d.get('best_candidate_id')}/{d.get('best_score')}\")
"
```

查看每个 candidate 的 iteration：

```bash
for f in "$RUN"/candidates/*/candidate.json; do
  python3 -c "
import json
d = json.load(open('$f'))
print(f\"{d['candidate_id']}: {len(d.get('iterations', []))} iterations\")
for it in d.get('iterations', []):
    print(f\"  iter {it['iteration']}: score={it['score']} failure={it.get('failure_class')} changed={it['changed_files']}\")
"
done
```

`feedback_policy=visible_to_workers` 时，失败 process verifier 向调用方返回有界 `stdout_tail`/`stderr_tail`；完整输出保存在该 iteration 独立的 `log_paths` 中，不会被后续调用覆盖。

### Git 与 `results.tsv`

```bash
find "$RUN/workspace" -name .git -execdir git log --oneline \;
find "$RUN/workspace" -name results.tsv -exec sh -c 'echo "=== $1 ==="; cat "$1"' _ {} \;
```

`results.tsv` 由 runtime 根据 `candidate.json.results_ledger` 生成、继承并只追加。每个成功返回 report 的 verifier 调用贡献一行：

```text
commit	combined_score	status	hypothesis
a1b2c3d	0.682	pass	baseline (concentric rings)
b2c3d4e	0.949	pass	hex lattice [5,4,5,4,5,3] s=0.1875
c3d4e5f	0.651	fail	switch to rectangular grid (regressed)
```

`commit` 是 verifier 实际测试的 artifact head，之后追加 ledger 的 commit 保存在 `ledger_git_head`。`keep` 后 workspace `HEAD` 通常比测试 snapshot 多一个 ledger commit；`discard`/`failure` 后 attempt commit 仍可按 hash 读取，但 workspace code 已恢复为 candidate-local best。

worker 不得手动编辑、删除或截断该文件，否则下一次 verifier 会报 `ResultsLedgerMutation`。旧 candidate 恢复时，legacy `.tmp/results.tsv` 和缺失的 verifier-backed iteration 会迁移为规范 ledger row。

## Host 原生日志

原始日志应放在 `.gp/host-logs/` 或其他 ignored 目录。它可能包含 prompt、tool input、command output、file content 或意外打印的 credential，绝不能提交。

关联日志时优先使用 launch payload 中的 `agent_session_id`、`candidate_id`、`host_handle.external_id` 或 `host_handle.task_name`。

### Codex

脚本化运行可直接捕获 JSONL event stream：

```bash
mkdir -p .gp/host-logs
codex exec --json --cd "$PWD" "<goal-plus prompt>" \
  > ".gp/host-logs/codex-$(date +%Y%m%d-%H%M%S).jsonl"
```

`codex exec --json` 包含 `thread.*`、`turn.*`、`item.*` 和 `error` event。`codex exec -o` 只保存最终答案，不能代替完整 trace；`--ephemeral` 会禁用本地 session rollout 持久化。

Codex 默认 transcript 位于：

```bash
find "${CODEX_HOME:-$HOME/.codex}/sessions" -name 'rollout-*.jsonl' -print
```

其他常用路径包括 `${CODEX_HOME:-$HOME/.codex}/session_index.jsonl`、`${CODEX_HOME:-$HOME/.codex}/archived_sessions` 和 macOS 的 `~/Library/Logs/com.openai.codex/`。需要 TUI log 时显式启用：

```bash
RUST_LOG=debug codex -c log_dir=./.codex-log
tail -F ./.codex-log/codex-tui.log
```

Codex 0.144.1 及更高版本的 Goal Plus hook 包括 `UserPromptSubmit`、`SessionStart`、`PreToolUse`、`PostToolUse`、`Stop` 和 `SubagentStop`。只有解析到 Goal Plus Goal、Search 或 session context 的自动 Stop/SubagentStop invocation，才会以不含内容的独立 JSON record 写入 `.gp/host-logs/codex-hook-events/`；其中保存 decision、耗时、有限 reason/error、stop reason 和解析到的 ID，不保存 prompt、transcript 或 tool payload。直接调用 `goal_plus_gate` 不会产生这些 record；disabled hook、runtime root 创建前收到的 Stop，以及没有解析到 Goal/Search/session context 的 invocation 也不会写入。

缺少这些 record 通常表示 hook 尚未被信任、event 在 `.gp` 创建前发生，或该 invocation 与 Goal Plus context 无关。candidate 的 `SubagentStop` 应绑定自己的 `agent_session_id` 并要求至少一次 verifier；一旦 session 的 `counters.verifier_runs` 为正，parent-only selection/report/promotion 就不应阻止 candidate 返回。

报告中的 Stop Hook Activity 和 Loop Agent Stop Outcomes 只依据持久 hook record、native session activity 和 candidate iteration。`polling-only`、`answer-only`、`empty-output` 等 tail label 是确定性诊断 heuristic，不代表模型意图，也不能证明因果关系。

### Pi RPC

Pi Search worker 由 `goal-plus-pi-worker` 启动。正常流程使用 host-local `pi_search_pool_*` supervisor：open 启动固定 lane，wait-any 返回 terminal event，continue 恢复同一 candidate/native session，snapshot 负责重新发现，close 负责 drain 或 interrupt。

runner 命令形态为：

```bash
pi --mode rpc --approve \
  --session-dir <root>/.gp/host-sessions/pi \
  --session-id <agent_session_id> \
  -e <repo>/.pi/extensions/goal-plus.ts
```

`.gp/host-logs/pi-rpc-<agent_session_id>.jsonl` 默认只保存 event/tool status、usage/count 和有界 error，不保存流式 message、prompt、reasoning 或 tool payload。只有短时聚焦排障确实需要完整 RPC payload 时才设置 `GOAL_PLUS_PI_RAW_LOG=1`；raw stream 体积大且可能包含敏感内容。

Pi native session 保存在 `.gp/host-sessions/pi/`，pool state 保存在 `.gp/host-pools/pi/`。`pi_search_pool_continue` 可以用新进程恢复同一 native session；Search runtime 中的 candidate Git、workspace 和 verifier iteration 始终是权威依据。runner failure 会绑定 synthetic failure handle 并产生 `subagent_runner_failed` warning，避免 session 被误判为仍在运行。

## 常见故障

### Worker 已结束但 Candidate 没有分数

先比较 `agent_sessions/<id>.json.counters.verifier_runs` 和 `candidate.json.iterations`，再检查对应 host log 是否调用过 `search_run_verifier`。worker 从未提交 verifier 时，main 可以对其留下的 workspace 调用 parent fallback verifier，但必须省略 `agent_session_id`。

### Agent 用 Bash 绕过 MCP Verifier

host log 中出现 `python evaluator.py` 等命令，但 runtime 没有对应 iteration，说明 agent 只运行了 evaluator，没有通过正式 scorer 结算。应修正 prompt/tool 使用，而不是从 shell output 手工补分。

### Subagent 仍在运行但需要停止

中断属于 host：Codex 使用 `interrupt_agent`，Pi 使用 `pi_search_pool_close(mode="interrupt")`。Search runtime 没有额外 abort API。若 verifier 已确认失效，应先 `search_invalidate_run` fence run，再停止全部 worker 并确认 active count 为零，最后修复 verifier、重新 freeze 并创建 successor run。

### `EditSurfaceViolation`

检查最新 iteration 的 `touched_denied_files`、`changed_outside_allowed` 和 `changed_files`。不要修改 frozen spec 或手工 reset 来绕过 edit surface。

### `VerifierWorkspaceSideEffect`

检查 verifier result 的 `metrics.verifier_workspace_side_effects`、`cleanup_failures`、`infrastructure_failure` 和 `candidate_action`。该错误表示 verifier 把 compiler output 或临时文件写进 candidate workspace。

worker 应立即停止并报告。main 修复 source-owned verifier，使临时输出使用 `GOAL_PLUS_VERIFIER_TMPDIR`、`TMPDIR` 或 `tempfile`；并发 verifier 不能共享固定 `/tmp` 路径。随后重新 freeze 并创建新 run，复用任何 candidate state 前先确认 `cleanup_failures` 为空。

## 只读诊断 API

| 工具 | 内容 |
|---|---|
| `goal_plus_monitor_snapshot(...)` | Goal/work DAG、Search、session、host 和 feature plugin 的组合视图 |
| `search_status(run_id)` | run state、candidate count 和 best score |
| `search_list_history(run_id, top_n, sort_by)` | candidate ranking 和研究汇总 |
| `search_list_iterations(run_id, candidate_id)` | 单个 candidate 可能很大的完整 verifier history |
| `search_get_agent_context(agent_session_id)` | 有界的 best/recent context、恢复字段和 `results.tsv` 路径 |
| `search_get_global_evidence(agent_session_id)` | 当前 Evidence mode 允许的 settled worker Evidence 和 View |
| `search_get_evidence_detail(agent_session_id, candidate_id, iteration)` | 按需 supplemental evaluation |
| `search_get_agent_observability(agent_session_id)` | 单 session 的归一化 model、timing、usage 和 artifact Evidence |

一个典型的跨层结论是：“host session 已结束，host log 中没有 `search_run_verifier`，同时 `candidate.json.iterations` 为空。”这能证明 worker 没有自行结算；仅看到 assistant 声称已经验证，不能替代这两层 Evidence。
