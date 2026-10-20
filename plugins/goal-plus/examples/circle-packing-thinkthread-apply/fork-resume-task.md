The preparation turn is complete. Resume this same Main session's existing Goal
through its host control, then immediately call `goal_plus_search_fork` for
the already prepared run, using the default `latest_completed` checkpoint. Do not
repeat host start, freeze, create, plan, or start_batch. Do not call
`goal_plus_search_start_agent_session` or redispatch: the fork result provides the
two branch `agent_session_id` values.

Use `goal_plus_session_run` and the returned controls for the two branches. Their
first turns must only copy their respective frozen hints to `initial_program.py`,
call `goal_plus_search_run_verifier`, and return. Then run a second turn on each
same Child: replace only the radius safety margin `1e-7` with `5e-8`, verify once,
and return. Keep every other file frozen; no numeric scan or third turn is needed.
Use four distinct deliveries in total and preserve each initial Child identity.

Close both branch sessions after their second turns. Wait for all required fork LLM
audit and Annotation Evidence, select the highest valid exact-snapshot score,
promote with its independent verifier, and apply to Root. After apply returns
`state=applied`, run the Root's frozen verifier directly; require `valid=true` and
`sum_radii >= 2.2`. Do not re-run a candidate verifier or repeat promotion after apply.
Record the Search result, finish the original Goal audit, mark complete, generate
MD/HTML reports, and complete execution-scope cleanup. Codex uses
`goal_plus_host_close_scopes` after the Goal is terminal; Pi uses its host lifecycle.
