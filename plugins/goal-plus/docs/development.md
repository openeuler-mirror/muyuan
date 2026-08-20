# Checkout Development and Shared Profiles

Use this guide when developing Goal Plus from a local Muyuan checkout,
refreshing host integrations after a source change, or keeping multiple
development versions isolated on one machine.

## Development surfaces

A development checkout has three update paths with different activation
boundaries:

| Surface | Source | Update action | Activation boundary |
|---|---|---|---|
| Python runtime | `src/goal_plus/` from an editable install | No package reinstall after the initial `pip install -e` | Restart any process or MCP server that already imported the old modules |
| Pi package | The registered local checkout path | No Codex plugin refresh | Start a new Pi process |
| Codex plugin assets | Staged copies of `skills/`, `hooks/`, `.mcp.json`, and `.codex-plugin/plugin.json` | Rerun `scripts/install_codex_plugin.py --skip-runtime-install` | Start a new Codex thread; retrust changed hooks before starting the final fresh thread |

Editing Python runtime code can therefore appear immediate when a command
starts a new Python process, while editing a Codex skill or hook does not mutate
the already-installed plugin cache.

---

## Set up an isolated development profile

Separate operating-system accounts are isolated by their home directories as
long as each developer also uses a separate Goal Plus checkout and Python
environment. When multiple developers or versions use one account, give each
one a complete host profile:

```bash
python -m venv /absolute/path/to/developer-a/venv
source /absolute/path/to/developer-a/venv/bin/activate
python -m pip install -e "/absolute/path/to/developer-a/muyuan/plugins/goal-plus[dev]"

export CODEX_HOME=/absolute/path/to/developer-a/codex-home
export PI_CODING_AGENT_DIR=/absolute/path/to/developer-a/pi-agent

pi install /absolute/path/to/developer-a/muyuan/plugins/goal-plus
python /absolute/path/to/developer-a/muyuan/plugins/goal-plus/scripts/install_codex_plugin.py \
  --skip-runtime-install
```

Keep the same Python environment, `CODEX_HOME`, and `PI_CODING_AGENT_DIR`
active whenever installing or starting Pi or Codex. Also use a separate target
workspace or Git worktree per developer because Goal Plus state lives under
that workspace's `.gp/` directory and agent edits affect the workspace itself.

An explicit source path selects a checkout only inside the active host profile.
It does not isolate a shared Python `PATH` or hide another Goal Plus
registration already enabled in the same profile.

---

## Refresh a checkout after changes

1. Activate the Python environment and host profile that own the checkout:

   ```bash
   source /absolute/path/to/developer-a/venv/bin/activate
   export CODEX_HOME=/absolute/path/to/developer-a/codex-home
   export PI_CODING_AGENT_DIR=/absolute/path/to/developer-a/pi-agent
   cd /absolute/path/to/developer-a/muyuan/plugins/goal-plus
   ```

2. For Python-only changes, restart any existing Pi process, Codex thread, or
   MCP server that may still have the old modules loaded. The editable install
   does not need to be repeated.

3. For Pi asset changes, start a new Pi process. Pi loads the registered local
   package path directly.

4. For Codex skill changes, synchronize the development and publishable skill
   copies before refreshing the installed plugin:

   ```bash
   python scripts/sync_codex_plugin_assets.py
   python scripts/sync_codex_plugin_assets.py --check
   ```

5. For any Codex static plugin-asset change, refresh the staged plugin and
   verify the active installation:

   ```bash
   python scripts/install_codex_plugin.py --skip-runtime-install
   goal-plus-codex-doctor --project-root .
   ```

The installer creates a new cachebuster and reinstalls the fixed development
identity `goal-plus@goal-plus-local`; it does not change the version committed
to the repository. Start a new Codex thread afterward. If hook content changed,
review and trust Goal Plus in `/hooks`, then start one more fresh thread so all
handlers are active from `SessionStart`.

---

## Shared-profile behavior

Within one shared profile, the Codex development installer uses the fixed
`goal-plus@goal-plus-local` identity and staging path, Pi may load every
registered package path, and `goal-plus` commands resolve from the inherited
`PATH`. A later install can therefore replace or coexist with an earlier one
instead of remaining isolated.

Refreshing Codex assets in a deliberately shared `CODEX_HOME` replaces that
same development plugin identity for everyone using the profile. Coordinate
the update when one shared version is intentional. Use separate profiles when
concurrent versions must coexist.

## Related documentation

- [Codex integration](codex.md)
- [Pi integration](pi.md)
- [Runtime debugging](debugging-runtime.md)
