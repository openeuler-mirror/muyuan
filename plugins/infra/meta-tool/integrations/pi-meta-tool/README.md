# Pi online (BATE)

This directory is a standalone Pi host adapter. Its Python JSON-lines transport
calls **this repository's** `bate.cli extract --source graph --also-raw
--naming stable-short`. It does not import or start another
algorithm repository. Branch splitting, determinism and compilation remain
owned by BATE; Pi adds execution admission and catalogue lifecycle handling.
An optional read-only CLI observer supplies aligned occurrence evidence to the
adapter. LLM-turn savings are calculated from recorded turn IDs, not inferred
from the number of tool calls. Default CLI output remains unchanged.

## One-command project installation

From the plugin root, use the same installer as Codex:

```bash
python3 install.py --pi --project /absolute/path/to/your/project
# Equivalent: bash install.sh --pi --project /absolute/path/to/your/project
cd /absolute/path/to/your/project
pi
```

Requires Python 3.11+, Node.js 22.18+, npm and Pi. The installer prepares a
project-local Python environment, copies this adapter, installs locked npm
dependencies, and registers it in `.pi/settings.json`. No manual `--extension`
is needed. Pi still applies its normal project trust rules. Use `/meta-tool`
in the session. Existing model settings and learning policy are preserved.
See the [Chinese guide](../../README_zh.md) for generated paths and conflicts.
Add `--codex` to install both hosts. Close running sessions before reinstalling;
both hosts share the Python runtime. Global host settings are not modified.

For development, the explicit source-loading route is still available (start
from the plugin root):

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cd integrations/pi-meta-tool
npm ci --ignore-scripts
pi --extension "$PWD/extensions/meta-tool.ts"
```

For Goal-Plus, load the selected Goal-Plus extension alongside this installed
package (or the source extension in development). The local `bin/pi` propagates it to native Pi workers. Validate worker
tool registration before using a real model. Do not also load a different
meta-tool package from global Pi settings.

Configuration lives in `<workspace>/.pi/meta-tool/`; runtime data uses that
directory by default or `.tmp/meta-tool/` with the private layout below. Three
bounded 2/3/4-read compositions are bundled and enabled by default for both
Static (`online.enabled=false`) and Online. They are preinstalled capabilities,
not newly discovered workflows. Set `routing.includeUserCatalog=false` to keep
unrelated user catalogs out of automatic routing; the bundled reads remain
available. The legacy normalization optimization option is disabled; enabling it is rejected rather
than silently switching algorithms. `/meta-tool` exposes collection, extraction,
catalogue inspection and execution commands.

Learning uses completed, successful task segments. Default extraction support
is three traces and Pi admission needs two distinct sessions. A cold-start K=1
experiment can therefore produce zero newly learned active tools. Extracted, admitted and
actually executed tools must be reported separately. Do not count ordinary
Goal-Plus calls as learned meta-tools.

Safety: unknown tools cannot be admitted. Read-only workflows use `safe_auto`;
mutating workflows retain interactive confirmation guards. Noninteractive
experiments do not bypass them. Source traces are private workspace artifacts
and may contain source code or tool outputs; do not publish them automatically.

The TypeScript lifecycle, journal, routing and transport code was copied from
the earlier Pi host integration into this repository (not linked at runtime).
The discovery adapter is new and has CLI-parity regression coverage.

Tests: `npm test` here and `PYTHONPATH=src python -m pytest tests/test_pi_online.py`
from the repository root.

For a real Pi main/worker-wrapper check without model calls, run
`python3 tests/smoke_read_batches.py --pi /path/to/pi` here. It checks default
Static/Online batching, explicit opt-out, and worker propagation using
isolated temporary workspaces.

## Goal-Plus 长任务与单次 Pi 运行

配置仍位于 `.pi/meta-tool/config.json`。使用 Goal-Plus 时，建议显式加入：

```json
{
  "runtimeLayout": "private",
  "online": {
    "segmentCalls": 32
  }
}
```

`private` 将日志、提取结果、目录和执行统计放到当前工作区的 `.tmp/meta-tool/`，
不改变任务的交付范围。Goal-Plus 的 inner-Git 标记存在时也会自动选择此布局。
普通 Pi 项目未启用该选项时继续使用原来的 `.pi/meta-tool/` 状态位置；不会搬动旧实验。

长任务在完整工具回合结束、累计达到 `segmentCalls` 后封存一段轨迹。
这种记录带有 `task_complete=false`，只说明该段原始工具调用的结果，不表示审计任务完成，
也不会增加独立 session 的数量。错误恢复和准入条件仍然生效。

在线发布后，会更新当前 Pi 的可用工具，下一模型回合即可看到新工具。
单次 print/JSON 运行退出时，会等待已有提取，并在配置的提取超时内处理尚未消费的新轨迹；
不会只设置一个退出时被取消的定时器。这里跳过的是批量等待时间，不是提取或安全准入门槛。

写入失败的待写记录会保留，后续刷盘可重试；插件只重建自己拥有的日志目录。
Main 和 candidate 的状态仍在各自工作区内，不通过扩大沙箱或读取其他项目日志来凑样本。

## 默认开启的批读（Static / Online / Pi worker）

正常加载 `extensions/meta-tool.ts` 即会注册 `meta_tool_read_batch`，并由现有
`bin/pi` 自动传播到 Goal-Plus 原生 Pi worker。Static 和 Online 都默认开启，
无需准备 user catalog 或先采集当前项目轨迹。批读由主扩展统一注册，
旧启动器需移除额外的 `extensions/read-batch.ts` 扩展参数。

仓库内置三个历史提取、已验证的 2/3/4-read 模板，位于
`src/bundled-read-batches.json`。它们沿用 2026-10-23 worker 批读实验的模板与证据，
来源 catalog SHA-256 为 `7cceb51b5fd9a3d3ed7a814bd7486d3b7f6cb394d87a3397006fbe75cde55858`。
模板不包含训练文件路径、代码或漏洞答案；运行目录中不计为 Online 新学工具。
用户目录中的同名模板优先于内置版本。

默认配置是 `admission.policy.allow_caller_bound_read_batches=true`。若要关闭，
在当前项目 `.pi/meta-tool/config.json` 中合并：

```json
{
  "admission": {
    "policy": {
      "allow_caller_bound_read_batches": false
    }
  }
}
```

配置和 `/meta-tool disable` 在下一模型回合前更新可用工具，执行时也会重新检查；
显式 `false`、未信任项目和关闭 meta-tool 的状态均会禁止批读。修改源码后在 Pi
执行 `/reload` 或重启会话加载新版本。

若需要重新提取自己的历史模板，`python -m bate extract-read-batches --traces traces.json
--out extracted` 可以从带 `turn_id`、`metadata.session_id` 的历史成功轨迹提取
2/3/4 次 `read` 的参数化模板。至少需要三个独立 trace、两个 session，且证据须跨模型轮次；
同一轮本来就并发的读取不算节省证据。计数可能重叠，是潜力上界，不是实测收益。

参数为
`ranges: [{path, offset, limit}, ...]`，数量 2–4、每段最多 300 行。
调用仍经标准 `meta_tool_run`、指纹校验和只读准入；保留每段返回内容及截断提示。
每个路径和范围都必须由调用方事先给定，模板不保存训练文件路径、代码或漏洞答案。

仅在已知多个待读范围时使用；下一步需要理解前一步结果时继续使用普通 `read`。
不自动增加读取，不替代审计判断、漏洞复核或停止决策。评估应分开报告 main/worker 的
LLM 响应数、总 token（包含 cache）、组合成功数、耗时和最终 F1，不能把工具调用减少
直接视作模型调用或 token 减少。这三个内置模板属于历史热启动能力；新的普通工作流
仍由原 BATE 提取和准入规则决定。Python CLI 的提取参数和通用 dispatcher 默认值不变。
