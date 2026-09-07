$goal-plus mode=probe max_parallel=2 max_duration_seconds=1200 workspace_backend=git_worktree promotion_mode=artifact_only strategy=agent_guided inner_agent=common Run the bounded native Codex Circle two-candidate and same-session continuation test.
<!-- markdownlint-disable-file MD013 MD041 -->

Use the installed goal-plus/search skills and current MCP schemas. This is an
authorized system test: do not run pytest, another host CLI or tests/st scripts.
Use native collaboration to launch the candidates from runtime launch payloads.

Read source/spec.json and adopt the complete SearchSpec, including model and
Annotation settings. Save the draft, freeze evaluator.py relative to source_path,
create and link exactly one run to the current Goal, then make exactly one K=2
plan with search_plan_next(requested_k=2) and start its batch.

The model work is deliberately bounded. Supply these candidate directives:

- c001: On the first turn copy candidate-hints/rectangular.py to initial_program.py.
  Submit one search_run_verifier with scope="process", your bound agent_session_id,
  and a concise hypothesis, then end the turn. On the next turn in this same native
  session, copy candidate-hints/staggered.py instead and submit one more verifier.
- c002: Copy candidate-hints/staggered.py to initial_program.py, submit one process
  verifier with your bound agent_session_id, and end the turn.

Only initial_program.py may change. Do not perform numerical optimization or add
candidate work beyond these edits. Launch both initial candidates using the exact
runtime payload and supported native schema, bind their handles, and wait for
terminal completion. Apply the returned host watchdog if a turn does not finish.

After c001's first verified turn, call search_continue_agent_session for its
existing agent_session_id, then followup_task on the SAME native agent using the
returned continuation message. Do not spawn a replacement. Wait for its second
verified turn and c002's first turn. No additional plan, candidate or run.

Wait for all required Annotation tasks to complete. Select the highest valid
exact artifact, promote it as artifact_only, record the Search result, complete
the original Goal review and set Goal complete, then generate search_report.
Drain/close native workers through the normal host lifecycle. Keep the Root source
unchanged. The test reads persistent state, native model observations and scope
cleanup itself; report actual identifiers and any failure briefly in the final.
