# Authorization and Derivation

Use when choosing a role, model, or Child derivation, or when an interface is absent/rejected.
Prerequisites are an actual Root binding and the installed host's supported interface. A successful
Root handshake does not grant its capabilities to Child roles.

## Check capability delegation

The Goal Plus Root binding checks TT child, message, own-filesystem and managed-filesystem
capabilities. Its `self` Profile must match the host and allow a fresh/private-branch derivation;
candidate delegation is narrower than Root authority. Inspect the effective Profile and actual
Child binding, not just a template. Runtime code owns these checks and role identity.

Choose models through the installed Goal Plus catalog and TT's exact provider/model delegation
allowlist. Main, candidate/annotator, and a domain's served model are different choices.
`--codex-profile` selects host configuration, not candidate authorization. Preserve requested
models when unavailable and consult
[support boundaries](../capabilities.md#establish-actual-support).

## Use the role-bound surface

| Role | Goal Plus entry/surface | Normal responsibility |
| --- | --- | --- |
| Main Root | Goal/Search/session tools; Codex TT host start and close-scopes | Prepare environment, freeze, dispatch, observe, select and deliver |
| Candidate Child | Candidate context and session-bound Search tools | Modify its private implementation and submit real verifier attempts |
| Verifier Child | goal_plus_thinkthread_verifier_input / goal_plus_thinkthread_verifier_submit | Read fixed assessment, submit structured result, end turn |
| Annotation Child | goal_plus_thinkthread_annotation_next, authorized Artifact View reads, goal_plus_thinkthread_annotation_submit | Assess returned immutable views; no command execution or workspace mutation |

Verifier Child assessment and a process verifier's `fs.run` invocation are different execution
paths. Diagnose the one that actually failed; their environments and permissions are not identical.
Read exact Goal Plus inputs through `goal_plus_get_tool_schema` on the authorized Main surface.

## Verify access and context

Check the role-bound tool listing, frozen task context, actual workspace/snapshot identity, and
task-local skill visibility. A static skill list or Main's access does not prove Child loading.
Keep role bindings host-owned; changing a prompt, Child id, or environment variable does not confer
authority. Candidates do not repair the parent Profile or shared runtime.

An absent tool may be expected for the role, missing host integration, or unsupported capability.
Compare those facts before changing configuration. Route credential/model rejection through
[binding diagnostics](../troubleshooting.md#installation-and-binding), and context visibility
through [snapshot diagnostics](../troubleshooting.md#snapshots-and-context).
