# Isolated Execution

Use for TT `fs.run`, task interpreters, verifier devices, output handling, and execution budgets.
Prerequisites are an authorized exact snapshot, supported execution interface, task dependencies,
and defined outputs. Goal Plus owns managed verifier invocation; direct probes cannot replace it.

Use the task's declared environment. Identify its activation script,
absolute interpreter/compiler, SDK/native runtime, libraries, model roots, and device allocation.

## Establish the toolchain

- Inspect the authorized setup source before executing it. Source it in a subprocess; activation
  can execute code even during a preview. Capture only explicitly needed non-secret variables.
- Verify imports and compiler versions under that same sourced environment, including dynamic
  libraries. Package metadata alone does not prove which files Root, Child, or verifier will load.
- Preserve the venv interpreter path rather than resolving its symlink to a base Python. Add the
  required interpreter/symlink targets to provider read access. Do not reinstall a user-supplied
  runtime simply to make an environment probe pass.
- Separate interpreter identity from implementation imports: a candidate must load its own
  implementation, while verifier/reference dependencies remain frozen and independent.
- Use task-specific probes: an import, bounded compile, then device execution only when required
  and authorized. Failed infrastructure probes block dependent expensive benchmarks.

Bare python/python3 in a Goal Plus verifier maps to the Goal Plus interpreter, not the shell PATH.
Use an absolute task interpreter (or a frozen wrapper that invokes it) for external dependencies.
The TT verifier sets PYTHONPATH, PYTHONHOME, and PYTHONNOUSERSITE itself. Main's shell and Profile
PATH do not change that. A wrapper may explicitly establish required task dependencies
under the verifier contract without changing Goal Plus's own runtime.

The wrapper must independently activate the environment on each verification and set build/cache
paths after sourcing, since activation can reset them. Validate temporary directory injection and
snapshot accessibility below; session-wide defaults do not isolate independent verifier attempts.

Prefer argument arrays with an explicit cwd; when sourcing requires a shell, pass task values as
positional arguments to `bash -c` instead of interpolating them into shell code.
Pin the intended working directory and shell mode for every probe. Login-shell wrappers such as
`bash -lc` may change cwd or load unrelated startup files; do not attribute the resulting path or
latency change to ThinkThread until the command is reproduced with explicit cwd and environment.
The task's model root, backend version, and device visibility are inputs to the task contract, not
ThinkThread defaults.

For host preparation, separate metadata/path discovery from dynamic import and native-library
checks. Reuse bounded no-model task probes; inspect their initialization side effects before
running them. Record the actual execution surface and loaded module/library paths. Imports can
touch drivers, so a successful host import is not device availability or TT sandbox evidence.
Source preparation must preserve device, lock and temporary variables supplied by the caller.

Read ABI or other machine-readable properties through a validated structured result, separated
from import/driver logs. Treating all stdout as one scalar can turn diagnostic output into a false
configuration failure. An environment switch used to isolate a metadata query must stay scoped to
that subprocess; it cannot replace the subsequent real extension/native-library loading check.
Reset only known task-library contamination when the probe requires it, then activate the declared
environment explicitly. Do not blanket-clear host/GP identity or resource bindings.

## Verify device and lock wiring

Read-only model caches may be shared. Keep candidate and verifier writes separate, including
builds, writable caches, service logs, ports, and profiler output. Git-ignored does not mean a path
is writable in a provider snapshot or will be included in promotion.

Retain authorized device mappings and account for process-local renumbering. Coordinate full
device groups with the task's resource locks, covering the full service lifetime when applicable.
Multiple workspaces or identical local device numbers are not evidence of physical isolation.

Check origin, candidate, and promotion separately against their intended frozen allocation.
Compare logical lane, effective visibility, physical resource identity, and launched process usage.
Match the resource identity, lock namespace and protocol across all contenders, including local and
TT verifiers. Equal `LOCK_DIR` and `GOAL_PLUS_VERIFIER_RESOURCE_LOCK_DIR` values do not suffice:
a handwritten task lock and GP's resource-hash lock can differ inside the same directory.

In the source integration underlying this skill, `run_thinkthread_command` in
`src/goal_plus/infrastructure/verifiers/thinkthread.py` explicitly supplies phase/Python variables
and passes the resource-lock string unchanged. It lacks explicit frozen device/TMPDIR injection.
This identifies missing wiring, not actual inherited values or a historical service's device.
Bind this finding to the actual installed build; later builds can repair individual paths. Verify
allocation injection, physical lock mapping and temporary-space support separately rather than
treating them as one all-or-nothing capability. Use a no-model probe before dependent expensive
work. Independent [preparation](../workflows/prepare.md) can proceed while that execution is blocked.
Do not infer TT behavior from `local.py`, which has a different environment/resource mapping path.

## Temporary space and results

Use `GOAL_PLUS_VERIFIER_TMPDIR` when supplied, validating its location against the frozen contract.
When outputs must remain outside the source snapshot, an invalid injected path is a wiring error;
do not silently choose another path. When no path is supplied and the wrapper may create one,
check its isolation, write access and cleanup in the provider. Host `/tmp` accessibility does not
prove `fs.run` accessibility. Share model inputs read-only and isolate writable caches per attempt.

The current Goal Plus TT verifier uses `writes="discard"`. Export bounded diagnostics through the
captured result or a supported artifact path before execution ends; check Main can read them after
discard. A path to discarded logs is not evidence. Respect capture limits and avoid raw environment
dumps. Candidate workspace outputs follow the separate [edit-surface rules](filesystem.md).

## Budget the complete invocation

Use the smaller effective limit of the installed provider and frozen verifier command:

~~~text
business budget + reserved cleanup budget + reporting/scheduling margin <= execution budget
~~~

Use one monotonic deadline for startup and all requests; each operation consumes remaining business
time. Cap cleanup within its reserved interval after failure, timeout, or handled termination.
Per-socket HTTP timeouts are not end-to-end deadlines. Sum worst-case step budgets and account for
cold starts; warm-run success does not justify a plan exceeding the execution limit.

Distinguish the caller MCP/bridge wait, provider timeout and verifier deadline. Increasing the
caller wait does not extend `fs.run`. Unknown completion requires original-request reconciliation
before retrying or releasing a resource; use [observation](observation.md).

## Verify or diagnose

Record sanitized executable/import identities, versions, source revision, commands, resources,
exit status, and the evidence path. Keep import, compilation, device correctness, benchmark,
and independent final verification claims separate.

A no-model probe can establish named environment values, write/read/export behavior, and source
immutability; it cannot prove model execution, physical device isolation, or cross-task exclusion.
Use [execution probes](../workflows/execute.md#choose-a-probe) for checks appropriate to the task.
Missing runtime wiring belongs in its owning source/build flow. Do not patch an installed release
or rewrite `.gp`; return a bounded report through [diagnosis](../workflows/diagnose.md).
