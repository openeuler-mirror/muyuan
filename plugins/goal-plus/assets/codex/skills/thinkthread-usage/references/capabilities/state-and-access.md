# State and Access Boundaries

Use for native state placement, role grants, shared locks or path permission failures. Preserve
the original state-root binding of a retained session.

## Shared resource locks

Check directory create/write/rename/remove access for the actual lock writer: Goal Plus updates
owners through same-directory temporary files and atomic replacement. A narrow temporary-file
probe can test access without acquiring a lease. All contenders must use the same resource names
and lock namespace. A new empty directory does not release old leases or prove devices idle;
coordinate a namespace change before relying on it. Do not grant a whole business repository
write access to fix one lock.

## Filesystem and state roots

Keep `GOAL_PLUS_ROOT` outside the Root source tree, with managed host/role access. Keep TT's
private native state root outside Agent/Child write grants and persistent when recovery matters.
A disposable `/tmp` is only for an explicitly short-lived attempt. Relocate a retained instance
only through supported export/recovery, not a directory move or `.gp` copy.

Check actual paths and UID/GID in Root, Child and snapshot execution; cwd and mounts can differ.
A Profile grant does not prove OS access or device allocation. POSIX permissions and Git ownership
are separate: repair the exact task-owned path or, when required, add exact `safe.directory`
trust. Avoid global Git trust and broad writable grants. Share model inputs read-only; assign
writable outputs and full device-group leases to the task.

For an old Root, use [native readiness and recovery](observation.md#native-readiness).
