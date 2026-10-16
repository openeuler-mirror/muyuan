# Configuration and Propagation

Use when preparing a workspace overlay or tracing a setting into Root, Child, MCP or verifier.
Confirm the [installed binding](binding.md) and task workspace first.

## Separate configuration ownership

- **TT/host Profile:** SDK and host wiring, role environment maps, MCP forwarding, native state.
- **Goal Plus contract:** Goal/Search state, provider, device registry, verifier lock and budgets.
- **Task environment:** interpreter, libraries, models, service and writable outputs.

A task shell or container image does not configure the TT Profile. Check required values at their
actual Root, Child, MCP and verifier boundaries; propagation between them is not automatic.
Inspect the installed Profile loader/schema before applying version-specific overlay rules.

## Configure a workspace overlay

For the schema-v4/alpha7 loader described by the original usage skill, the workspace matching
the `--fs` root can contain `.thinkthread/goal-plus-pi.toml` or
`.thinkthread/goal-plus-codex.toml`. Confirm support in the installed loader; do not force
schemaVersion=4 on another runtime.

1. Inspect the managed Profile and existing overlay. Retain the previous task-owned file for
   rollback. When refreshing a build, get a fresh base without the old overlay.
2. Use a TOML parser/writer to preserve full Root/Child environment maps, delayed session values,
   role bindings and grants. Merge paths deliberately; add only required dependencies and
   task-local writable directories. Keep credentials in host authentication.
3. Review a redacted diff, parse and apply atomically, then reread the effective Profile from the
   same workspace. Restore the prior file on validation failure.

Keep project-specific paths in the workspace overlay, not the managed Profile. Keep absolute
machine paths local. A refreshed base needs a rebased overlay and a new Root; retain the original
configuration for existing sessions. There is no bundled generic overlay generator.

## Check effective destinations

- Resolve interpreter, editable-package source, activation dependencies and symlink targets under
  actual read grants. Package metadata or the interpreter directory alone is insufficient.
- Register device inventory through `GOAL_PLUS_DEVICE_POOLS` when required; registration is not
  allocation. Verify shared-lock location and writer access in [state and access](state-and-access.md).
- For device-backed Codex TT, check `mcp_servers.goal-plus.env_vars` in the selected host config.
  Root/Child values alone do not forward `GOAL_PLUS_DEVICE_POOLS` or
  `GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR` to MCP. If a supported local override is needed, retain
  provider/model, MCP, authentication references and skill discovery; rebase it after a refresh.

Record only named, non-secret effective values. Parsing a Profile does not prove execution.
Use [isolated execution](execution.md) for imports, devices and temporary outputs.
