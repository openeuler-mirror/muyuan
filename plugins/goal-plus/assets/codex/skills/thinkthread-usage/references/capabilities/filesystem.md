# Filesystem and Snapshots

Use for workspace isolation, snapshot contents, edit-surface failures, or artifact delivery.
Prerequisites are the actual attachment and returned branch/snapshot identities, plus authorized
read/write paths. Source cwd, branch id, snapshot id, and native session id are not interchangeable.

## Inspect attachment and capacity

TT filesystem operations expose attachment state (`fs.stat`), branch state, snapshot metadata,
bounded reads/diffs, and request status. Goal Plus expects its Root filesystem directly attached
and its private runtime directory outside that tree. Inspect available active-request/snapshot
capacity before attributing admission failure to model or code behavior.

Use supported metadata and bounded read/diff operations on the exact artifact. A matching cwd or
`local_workspace` diagnostic flag does not establish shared storage. Actual Child/branch identities,
controlled writes and source immutability establish the relevant isolation claim.

## Work inside the frozen surface

Candidate changes belong to the private workspace and frozen edit surface. A Git-ignored file can
still change a TT snapshot. Put preflight/log output in authorized temporary space outside the
captured source tree, or admit its workspace path in the contract before freezing.
Keep verifier/reference inputs independent of candidate-editable implementation and configuration.

When comparing an attempt, use its baseline and attempt snapshots, not current live files.
Read only the required paths and bounded diff pages. Check whether a failure concerns a candidate
change, source capture, or verifier-side transient writes before changing the contract.
See [execution](execution.md#temporary-space-and-results) for discard and temporary output handling.

## Deliver the verified artifact

TT snapshot/apply facilities do not independently choose a Search winner. Goal Plus owns
selection, promotion, and publication verification. Use `goal_plus_search_select`,
`goal_plus_search_promote`, and authorized `goal_plus_search_apply_promotion` on settled artifacts.
Keep any pending verification and host gates visible.

`artifact_only` returns an artifact, not a source update or running service. For apply, inspect
the publication result and exact revision/artifact; do not copy a candidate directory over source.
Promotion does not promise to carry ignored logs or task-private state into the destination.

## Verify or diagnose

A read/diff probe succeeds when it identifies the intended immutable content within access and
output limits. A publication succeeds only with the matching public result and required verifier.
Private branch reset/remove and snapshot cleanup belong to their host/runtime owner in managed
work; a raw filesystem operation is not permission to recover a Goal Plus candidate checkpoint.

For unexpected contents, consult [snapshot symptoms](../troubleshooting.md#snapshots-and-context).
For capacity exhaustion, inspect retained owners and supported release paths through
[observation](observation.md); do not delete active snapshots or private state to free space.
