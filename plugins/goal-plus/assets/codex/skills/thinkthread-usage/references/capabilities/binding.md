# Installation and Binding

Use when checking the selected host, TT Profile, SDK, or installed Goal Plus build. For a retained
session, preserve its original binding; an installation refresh does not recover it.

## Inspect and install the binding

Check `tt --version`, `tt profile show <registered-alias>`, and the actual host. Use the managed
selector at the installed path, adjusting GOAL_PLUS_INSTALL_HOME when set:

~~~bash
~/.local/share/goal-plus/bin/goal-plus-info pi --thinkthread
~/.local/share/goal-plus/bin/goal-plus-info codex --thinkthread
~~~

Compare the registration with Pi `/goal-plus-info` or Codex `goal-plus://runtime/info` inside the
selected host. Record its build and runtime root; cwd and a checkout version are not proof.

From the owning checkout/release, `install.sh --pi --thinkthread` or
`install.sh --codex --thinkthread` prepares Goal Plus registration and Profile, not TT itself.
Use both host flags for both registrations. `--pi-dev` runs editable Pi without a TT Profile and
cannot be combined with `--pi`; combine it with `--codex --thinkthread` for managed Codex TT.
`--runtime-info` or `--env-show` reads the binding. `--codex-profile NAME` projects the user's
`NAME.config.toml`; `--agent-posix-source PATH` or
`--agent-posix-package PATH` selects the SDK when needed. Check the resolver and interface,
not just a version label. `--dry-run` may bootstrap uv/Python; `--yes` replaces an installation
only within the authorized scope.

The maintained TT execution examples target Linux; reading this skill on Windows does not prove
local TT availability. After a refresh, recheck [effective configuration](configuration.md).
For startup/SDK errors, use [the symptom index](../troubleshooting.md#installation-and-binding).
