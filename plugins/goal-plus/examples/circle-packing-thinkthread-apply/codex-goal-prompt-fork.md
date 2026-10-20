First call `goal_plus_host_start`, passing the complete task below, from `mode=probe`
onwards, as `raw_goal`. This is a ThinkThread Codex Root; use its trusted host tools.

spawn_mode=fork mode=probe max_parallel=2 max_duration_seconds=1800 workspace_provider=thinkthread promotion_mode=apply strategy=agent_guided inner_agent=common Complete the fork Circle Packing task in search-spec.json.

Use the complete SearchSpec prepared with `prepare_workspace.py --fork`:
`strategy.initial_context=fork`, `strategy.inner_agent=common`, and
`strategy.llm_validation=required`. Preserve its Annotation policy, ThinkThread workspace,
apply mode, and frozen `.goal-plus-verifiers/circle_packing_score.py`. Freeze it with
`agent_harness=codex` and `runtime_provider=thinkthread`.

The full Goal is one K=2 Search with exactly two branches forked from one completed
Main checkpoint. Branch c001 first copies `candidate-hints/rectangular.py` to
`initial_program.py`; c002 first copies `candidate-hints/staggered.py`. Each branch
immediately calls `goal_plus_search_run_verifier` and ends its first turn. Then dispatch
a second turn to each same Child: change only the radius safety margin from `1e-7` to
`5e-8`, verify once, and return. Four separate candidate deliveries, two per retained
Child, are required. After both branches close, wait for required fork LLM audit and
Annotation Evidence, select the best valid exact snapshot, independently verify the
publication, and apply it to Root. Re-run the Root verifier and require `valid=true`
and `sum_radii >= 2.2`. Audit the complete Goal, mark complete, close its execution
scopes with `goal_plus_host_close_scopes`, write MD/HTML reports, and finish cleanup.
Do not add another run, plan, candidate, or fresh worker.

This first Main turn is preparation only: freeze, create the run, plan, preflight,
and call `goal_plus_search_start_batch` once with exactly two proposals carrying the
branch contracts above. Do not call `goal_plus_search_fork` or any candidate
session tool yet. After the batch exists, set the Goal status to `blocked`, with reason
`fork checkpoint ready` and evidence naming the run and both candidates. End
this turn and report ready. Preserve the full task above as the Goal objective; the
user will resume this same Main session after its native checkpoint is complete.
