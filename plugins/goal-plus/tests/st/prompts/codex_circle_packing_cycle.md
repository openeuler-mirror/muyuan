$goal-plus mode=probe max_parallel=2 max_duration_seconds=1200 workspace_provider=git_worktree promotion_mode=artifact_only strategy=agent_guided inner_agent=common Complete the bounded Circle Packing task described by source/spec.json.
<!-- markdownlint-disable-file MD013 MD041 -->

Use evaluator.py as the frozen verifier, with the model and Annotation settings
in the spec. Run exactly two independent candidates:

- c001 first delivers candidate-hints/rectangular.py as initial_program.py, then
  reuses the same session to deliver candidate-hints/staggered.py on a second turn.
- c002 delivers candidate-hints/staggered.py as initial_program.py once.

Each candidate turn makes one edit, verifies it and returns. Only
initial_program.py may change. These three deliveries are the complete scope;
no numerical optimization, replacement candidate or additional run is needed.

Review their exact Evidence and required Annotation results, independently
validate the best publication and deliver artifact-only output. Keep the source
unchanged, complete the Goal review and generate Markdown/HTML reports. Close all
owned resources and give a final reply with the actual result and any failure.

This is an authorized native system test. Do not invoke pytest, tests/st scripts,
another host CLI or a handwritten controller to perform the task.
