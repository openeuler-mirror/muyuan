# Native Main Prompt Contracts

[codex_circle_packing_cycle.md](codex_circle_packing_cycle.md) is a bounded
native Main task: c001 makes two deliveries in one session, c002 makes one in
another session, and Main reviews Evidence before independent publication.
It is a test input, not a user-facing optimization example or shared runtime prompt.

The [Circle helper](../helpers/circle_cycle.py) validates persisted Goal/run state,
two exact native session identities, the required worker Evidence and Annotation,
artifact-only publication, unchanged source, reports and scope cleanup. The model's
final prose does not replace these checks. There is no required `st_report` output
block and no four-session/two-batch or `task_names` contract.

New scenarios should define acceptance in their owning helper or typed result.
Keep only task intent and normal operation in the model prompt; do not add a second
handwritten schema that can drift from the authoritative state.
Testing prerequisites and opt-in rules are in [Tests](../../README.md).
