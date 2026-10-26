# Capability Guide

Read the relevant capability page only when the selected workflow needs it. This maps TT
capabilities used by Goal Plus; it is neither a full TT API reference nor a benchmark guide.

## Capability map

| Capability | TT concepts and interface families | Goal Plus connection |
| --- | --- | --- |
| [Installation and binding](capabilities/binding.md) | Host registration, Profile, SDK binding | Selected host and build identity |
| [Configuration and propagation](capabilities/configuration.md) | Profile overlay, role environments, MCP forwarding | Three configuration planes and task dependencies |
| [State and access](capabilities/state-and-access.md) | Native state root, grants, path access | Persistent state and shared resource locks |
| [Authorization and derivation](capabilities/authorization.md) | Delegated capabilities, profiles, fresh/private derivation | Role-specific tools and model selection |
| [Sessions and communication](capabilities/sessions.md) | Child, message, wait, signal, lifecycle | Managed session calls, receipts, continuation and quiescence |
| [Filesystem and snapshots](capabilities/filesystem.md) | Attachment, branch, snapshot, read/diff, apply | Source isolation, edit surface, artifacts and publication |
| [Isolated execution](capabilities/execution.md) | fs.run, invocation environment, limits and write policy | Toolchains, verifier resources, temporary space and output export |
| [Observation and recovery](capabilities/observation.md) | Object/request state, readiness, quotas, retained state | Monitor, request reconciliation, host recovery and evidence |

Device allocation/locks, Search scoring, and service acceptance are Goal Plus or task obligations.
Neither a TT filesystem capability nor a visible device establishes those guarantees.

## Establish actual support

For the capability needed now, distinguish runtime availability, Goal Plus integration, role
authorization, and actual validation. Record the selected host/build and classify missing evidence
as unverified; distinguish it from an explicit unsupported capability or missing configuration.
Successful parsing/handshake does not establish Child execution or service correctness.

The following limitations describe the source integration underlying this skill. Check the installed
build and public contract before applying them to a retained session or declaring an upgrade fixed.

| Area | Current boundary | Decision |
| --- | --- | --- |
| Codex TT Root entry and continuation | Host start entrypoint; no ordinary Codex $goal-plus resume/Stop-hook continuation | Use [sessions](capabilities/sessions.md); do not promise automatic Main wakeup |
| Codex TT worker reasoning/service tier | Explicit reasoning_effort/service_tier overrides are rejected | Preserve requested intent and report unsupported settings |
| Codex TT Root delegation/final checker | Ultra delegation and independent final-check integration are unsupported/unverified in the maintained Codex guide | Do not substitute Main self-review for the required independent check |
| TT candidate checkpoint recovery | Provider recover capability is false | Use retained session/request reconciliation; see [recovery](capabilities/observation.md) |
| TT snapshot verifier wiring | Explicit allocation/TMPDIR injection is absent and the resource-lock string is passed unchanged | Probe the actual environment; see [execution](capabilities/execution.md) |
| Physical devices and process cleanup | Task/host responsibilities, not proved by a pool registration or a snapshot | Check execution evidence against the task's resource contract |

Version numbers and protocol fingerprints alone are not support tests. Use the installed interface,
typed contract and actual receipts; old logs and another release's presence are diagnostic context.

Use `goal_plus_get_tool_schema` for exposed Goal Plus inputs. Consult the selected TT interface for
native operation parameters rather than turning these pages into a second copy of the API schema.
For source changes, use the separate [development guide](development.md).
