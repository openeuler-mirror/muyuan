---
name: meta-tool
description: Control BATE meta-tools in the current Codex connection, inspect their status, and use suitable composed workflows or planned batch reads. Use when the user asks to enable, disable, or manage meta-tool acceleration.
---

# Meta-tool

Find this plugin's `meta_tool_control` MCP tool (the host may prefix its name).
Use it for runtime changes; a textual promise does not change the runtime.

Interpret the requested operation:

| Request after `$meta-tool` | Control action |
| --- | --- |
| no argument, `status` | `status` |
| `on`, `enable`, 开启 | `on` |
| `off`, `disable`, 关闭 | `off` |
| `static` | `static` |
| `online` | `online` |
| `batch on` / `batch off` | `batch_on` / `batch_off` |

Call the control tool with `{"action": "..."}` and report the returned enabled
state, mode and batch state. `static` freezes learning while retaining the
current catalog. `online` enables learning from operator-configured trace files.
`off` stops execution and learning after in-flight work finishes. State belongs
to this MCP connection and resets to installation defaults on restart; it does
not control other conversations. Do not re-enable after an explicit off unless
the user asks. With no operation, show status without enabling anything.

If Online returns `online_sources_not_configured`, explain that explicit rollout
paths must be added to the installation's config and the server restarted. Do
not scan unrelated conversation history or invent session evidence. A default
installation supplies project read batching; it does not hook native shell or
patch calls. Built-in read templates are not online learning results.

When enabled and relevant to the user's task, use `meta_tool_search`, then
`meta_tool_run` with the returned full fingerprint and matching inputs. Use
`meta_tool_read_batch` for 2–4 already-known file ranges, at most 300 lines each.
If deciding a later range requires an earlier result, read separately. Preserve
returned truncation indicators. Ordinary Codex tools remain available.

If these MCP tools are unavailable, report that installation/loading is incomplete
and ask the user to check `/mcp` or restart the trusted project session. Do not
claim activation or change host permissions to make a missing server work.
