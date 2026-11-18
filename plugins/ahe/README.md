# AHE — Agentic Harness Engineering

一条命令优化任何 harness：单个 prompt 文件、任意多文件 harness 目录。AHE 是
外层优化控制器：冻结被测对象，在隔离 workspace 里生成候选 patch，用真实评测器
打分，只保留通过门禁的候选。

> 快速上手流程见 [QUICKSTART.md](QUICKSTART.md)。
> 本插件是自包含的通用引擎：评测任务通过 `--eval-task`/`--eval-cmd`
> 与 manifest 声明，不内置任何特定 benchmark 或宿主 harness 的适配。

## 安装与使用

本插件自包含，仅依赖 Python ≥ 3.10 标准库。三种使用方式共享同一引擎与
同一套 `/ahe` 语法：

| | Pi（推荐） | Codex | 独立 CLI |
|---|---|---|---|
| 安装 | `bash install.sh --pi` | `bash install.sh --codex` | 无需 |
| 卸载 | `bash uninstall.sh --pi` | `bash uninstall.sh --codex` | 无 |
| 使用命令 | `/ahe <args>` | `/ahe <args>` | `python3 plugins/ahe/ahe.py <flags>` |
| 参数解析 | Pi 扩展代码（ahe.ts） | prompt 指导模型解析 | CLI flags |
| LLM API | 自动继承 Pi 会话配置 | 自动读 Codex 登录配置 | `LLM_*` 环境变量 |

安装器是同一个 `install.py`（`install.sh` / `uninstall.sh` 是等价的 bash 包装），
`--pi` / `--codex` 选择目标宿主，通用选项：`--status`、`--uninstall`、`--dry-run`。

### 作为 Pi 插件使用（推荐）

前置条件：已安装 Pi（pi-coding-agent）。执行一次注册即可将 AHE 登记为
Pi 插件（写入 Pi 的 `settings.json` packages 列表），此后所有正常启动的
Pi 会话均自带 `/ahe` 命令：

```bash
python3 plugins/ahe/install.py --pi      # 或 bash plugins/ahe/install.sh --pi
```

安装器行为：

- 注册位置由 `PI_CODING_AGENT_DIR` 决定，默认 `~/.pi/agent/settings.json`
- 原文件先自动备份为 `settings.json.ahe-backup`
- `--pi --status` 查看注册状态；`--pi --uninstall` 取消注册（不影响其他
  配置，等价于 `bash uninstall.sh --pi`）；`--dry-run` 只打印不写入

使用方式：以任意常规方式启动 Pi（仅需不带 `--no-extensions`），即可直接
输入 `/ahe` 命令（语法见下节）。整个流程自动执行：预检安装（缺失时才
安装）→ 评估器与
keep 主指标按任务自动生成（`AHE-OBJECTIVE` 写入 manifest）→ baseline →
N 轮优化循环 → `--export-best` 导出最终版并给出使用命令。

如不希望注册，可通过 `bash plugins/ahe/start-pi-ahe.sh` 启动一个仅包含
`/ahe` 的隔离 Pi 会话（内部同样通过 `--extension` 加载，效果等同）。

### 作为 Codex 自定义命令使用

前置条件：已安装 Codex CLI。执行一次安装即可将 `/ahe` 注册为 Codex 自定义
prompt，此后所有 Codex 会话均自带该命令：

```bash
python3 plugins/ahe/install.py --codex    # 或 bash plugins/ahe/install.sh --codex
```

安装器行为：

- 渲染 `assets/codex/prompts/ahe.md` 模板（将 `ahe.py` 引擎绝对路径写入
  prompt）并写入 `<codex home>/prompts/ahe.md`——支持自定义 prompt 的
  Codex 版本会把 prompts 目录下的每个 `.md` 注册为 `/` 斜杠命令；
  注意 0.14x–0.166 实测已不再自动加载该目录，`/ahe` 是否注册以本机
  codex 实际行为为准
- 位置由 `CODEX_HOME` 环境变量（或 `--codex-home` 参数）决定，默认 `~/.codex`
- 已有 ahe.md 先备份为 `ahe.md.ahe-backup`
- `--codex --status` 查看状态；`--codex --uninstall` 卸载（仅删除
  `prompts/ahe.md`，不影响其他 prompt，等价于 `bash uninstall.sh --codex`）；
  `--dry-run` 只打印不写入
- 引擎目录移动后，重新执行一次安装命令即可刷新路径

使用方式：安装后重启 Codex，支持自定义 prompt 的版本即可使用 `/ahe` 命令，
语法与 Pi 完全一致（见下节）；新版 codex 若未注册斜杠命令，可将渲染后的
`prompts/ahe.md` 内容直接粘贴进会话，或以独立 CLI 方式驱动引擎
（`python3 plugins/ahe/ahe.py --help`）。

实现差异：Pi 端为 TS 扩展，由代码解析参数；Codex 端 `/ahe` 以自定义
prompt 实现——Codex 依据 prompt 中的语法表解析同一段自由文本，
再按与 Pi 扩展一致的流程驱动 `ahe.py`（预检 → baseline → 评估器生成审阅 →
优化循环 → patch 报告/导出）。引擎、keep 门禁与产物完全一致。

### /ahe 参数语法（Pi 与 Codex 通用）

```text
/ahe <harness 路径或命令，自动识别>, <任务描述>, <目标/指标>, N轮
```

示例（harness 以命令引用，任务与目标为自由描述）：

```text
/ahe /my-tool, 完成漏洞检测任务, token降30%, 时间降20%, 准确率不变, 3轮
```

harness 段可为**路径**（`/data/.../my-tool`）或**命令**（`/my-tool`，
由 agent 定位提供该命令的插件源码目录）；任务与目标为自由文本，自动归位；
指标段（`token降N%`/`时间降N%`/`准确率不变`/`N轮`）可出现在任意位置。无
benchmark 时，可改用 `提示词为任务描述` 代替 `eval任务为...`。

`/ahe` 支持的可选段（均写在同一条命令中，以逗号分隔）：

| 段 | 示例 |
|---|---|
| `eval任务为<路径>里面的<项目>` | 评测任务：benchmark 只读目录 + 目标项目（如 `<benchmark根>/里面的<项目>`）；通过 `AHE_EVAL_TASK_ROOT`/`AHE_EVAL_TASK_PROJECT` 传给 evaluator，`--task-root` 自动带上 |
| `生成评估器` | 未提供评测器时按任务自动生成评估器 + keep 主指标（`--generate-evaluator`）；给了 `评测器为...` 则跳过 |
| `manifest路径为...` | 组件模式 manifest；不写则自动找 `harness.manifest.json` |
| `评测器为...` / `holdout评测器为...` | 真实评测命令；eval任务 模式下 agent 会从 benchmark 自带的 runner/scorer 中选择 |
| `任务根目录为...` | 只读任务仓库（discovery 不会把它放进可变面） |
| `自动发现adapter` | 只生成 manifest 草稿，不进入优化循环 |
| `harness路径为...`、`提示词为...`、`token降N%`、`时间降N%`、`准确率不变`、`N轮` | 基本段 |

### LLM API 配置（evolve / eval / debugger 共用）

按以下顺序自动回退，先命中先用；在 Pi 或 Codex 中运行 `/ahe` 时通常无需
任何配置：

1. 显式参数 / `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` / `EVOLVE_LLM_MODEL` 环境变量
2. `AHE_ENV_FILE` 指定的 env 文件（支持 `LLM_*` 和 `CSI_*` 键）
3. **发起任务的 Pi 会话**：Pi 会向 bash 子进程注入 `PI_PROVIDER`/`PI_MODEL`，
   ahe.py 据此从 Pi 的 `models.json`/`auth.json` 读取该 provider 的
   baseUrl/apiKey。在 Pi 中运行 `/ahe` 时通常直接命中此项，无需配置
4. **本机 Codex 安装**：`~/.codex/auth.json`（OPENAI_API_KEY）+ `config.toml`
   （model、`model_providers.<id>.base_url`）。在 Codex 中运行 `/ahe` 时
   通常直接命中此项，无需配置
5. 内置的本地 env 文件（见 `ahe_engine/api_config.py` 的 `load_api_config`）

### 独立运行

```bash
python3 plugins/ahe/ahe.py --help
```

唯一依赖是进化模型的 LLM API（纯 prompt 模式下同时兼作评测器）。

```bash
export LLM_BASE_URL=http://<gateway>/v1
export LLM_API_KEY=<key>
export LLM_MODEL=<evolve 模型>          # 提 patch 的模型
export EVOLVE_LLM_MODEL=<同上或更强>     # 未设时回退 LLM_MODEL
export EVOLVE_REASONING_EFFORT=max      # 可选
export LLM_WIRE_API=responses           # 端点是 Responses API 时设置
```

## 三种运行模式

### 模式 A：优化单个 prompt 文件（最简，LLM 评测）

harness 为单个 prompt 文件，评测方式为：由 LLM 回答测试题并进行子串匹配：

```bash
python3 plugins/ahe/ahe.py my-prompt.txt \
  --task "回答问题" \
  --test "2+2=?|4" "法国首都?|巴黎" \
  --rounds 3
```

此模式无需 manifest 与评测脚本。keep 条件：准确率不降且 token/时间改善。

### 模式 B：优化任意多文件 harness（组件模式，通用）

这是优化任意多文件 harness 的标准方式，包含三个要素：harness 目录、
manifest（声明可变面）与真实评测命令。若不熟悉 manifest 的编写，可先使用
自动发现：

```bash
python3 plugins/ahe/ahe.py /path/to/harness \
  --discover-adapter --task-root /path/to/task \
  --task "优化 harness 完成任务" \
  --eval-cmd "python3 /path/to/eval.py" \
  --output-manifest /tmp/ahe-auto-manifest.json
```

**修改范围对齐 AHE 论文**（arXiv:2604.25850，NexAU 框架）：manifest
应覆盖 harness 实际拥有的 7 类可编辑组件——**system prompt、tool
description、tool implementation、middleware、skill、sub-agent
configuration、long-term memory**。论文的消融实验表明收益主要集中在
tool implementation、middleware 和 long-term memory（system prompt
单独改反而回退），所以可变面不要只锁 prompt 一类。自动发现按这个
分类识别高置信度组件，任务代码、kernel、evaluator、golden 数据一律
排除；生成的 manifest 人工确认后再运行优化循环。详见
`skills/auto-harness-adapter/SKILL.md`。evolve 阶段允许并鼓励上网
搜索相关 skill、中间件模式与相关论文（含 AHE 论文本身）作为修改
参考；参考只提供思想，patch 锚点必须来自当前组件文件。

**0）运行前预检（去源目录确认安装与运行方式，只读）**：

```bash
python3 plugins/ahe/ahe.py /path/to/my-harness/ --preflight \
  [--manifest /path/to/manifest.json]
```

报告安装入口（manifest `installation.command` > `install.sh` > `install.py` >
`package.json` > `pyproject.toml`）、依赖状态（node_modules/.venv/manifest
`validation.command` 运行探针）和运行方式（adapter、评测入口、README 摘要）。
未就绪时应先从源目录安装，完成后再以 `--baseline-only` 进行真实验证：

```bash
python3 plugins/ahe/ahe.py /path/to/my-harness/ --install-harness   # 同命令换 flag
python3 plugins/ahe/ahe.py /path/to/my-harness/ --baseline-only ...
```

安装产物落在源目录，后续每个候选快照都会带上它们；baseline 失败疑似缺依赖时
引擎也会主动提示这两条命令。

**1）编写 manifest（声明哪些文件可被进化模型修改）**：

```json
{
  "$schema": "./manifest.schema.json",
  "name": "my-harness",
  "version": "1.0",
  "description": "任意 harness：模板、策略、运行时均可注册",
  "patch_mode": "structured",
  "baseline_frozen": true,
  "adapter": {"name": "generic", "command": "$CMD"},
  "components": [
    {"id": "prompts", "kind": "prompt", "path": "prompts", "mutable": true},
    {"id": "runtime", "kind": "runtime_core", "path": "src", "mutable": true},
    {"id": "config", "kind": "config", "path": ".", "mutable": true}
  ],
  "validation": {
    "command": ["python", "-m", "compileall", "-q", "src"],
    "timeout_s": 600
  },
  "evaluation": {"metrics": ["accuracy", "tokens", "time_s"]}
}
```

组件路径相对 harness 根目录；只有 `mutable: true` 的组件会进入进化上下文和
patch 白名单。`validation.command` 在每个候选落盘后、评测前执行，编译/导入
失败直接判废，不浪费评测预算。

**2）编写评测命令（eval-cmd）——或由 AHE 根据任务自动生成**。手写评测器
遵循以下约定；亦可先运行生成器（由进化模型根据任务 + manifest + eval 任务
结构生成符合契约的评估器，并将 AHE-OBJECTIVE 主指标写入 manifest，keep
门禁自动跟随）：

```bash
python3 plugins/ahe/ahe.py /path/to/my-harness/ \
  --manifest /path/to/manifest.json \
  --task "优化 benchmark 表现" \
  --eval-task /path/to/benchmark --eval-project <目标项目> \
  --generate-evaluator        # 输出 ahe_evaluator.py，审阅后作为 --eval-cmd 使用
```

AHE 对每个候选设置环境变量后执行评测命令：

```bash
python3 /path/to/eval.py
```

评测器约定（从环境变量读输入，stdout 最后一行输出 JSON）：

- 输入：`AHE_HARNESS_ROOT`（候选目录，评测对象）、`AHE_TASK`、`AHE_TRACE_DIR`
  （写产物）、`AHE_COMPONENT_MANIFEST`、`AHE_EVAL_STAGE`（`dev`/`holdout`）
- 输出：stdout 最后一行 JSON，至少包含主指标；建议同时给 `tokens`、`time_s`。
  generic 适配器的主指标是 `accuracy`

```python
# eval.py 最小实现
import json, os, subprocess, sys, time
cand = os.environ["AHE_HARNESS_ROOT"]
t0 = time.time()
r = subprocess.run([sys.executable, "run_benchmark.py"], cwd=cand,
                   capture_output=True, text=True, timeout=3600)
accuracy = parse_accuracy(r.stdout)          # 自定义评分逻辑
print(json.dumps({"accuracy": accuracy, "tokens": 0, "time_s": time.time() - t0}))
```

**3）启动**：

```bash
python3 plugins/ahe/ahe.py /path/to/my-harness/ \
  --manifest /path/to/manifest.json \
  --task "优化 benchmark 表现，准确率不降，token 与时间下降" \
  --eval-cmd "python3 /path/to/eval.py" \
  --rounds 5 \
  --workspace-dir /path/to/candidates \
  --trace-dir /path/to/traces
```

可选：

- `--holdout-eval-cmd`：独立 holdout 评测器，keep 前二次验证
- `--baseline-only`：只测 baseline 不进化（组件模式不要求 LLM API）
- `--no-write-back`：被接受的候选只留在 workspace，不覆盖源目录
- `--debug-cmd`：失败候选的诊断器（不设则用内置 LLM debugger）
- `AHE_REUSE_BASELINE=<file>`：复用历史 baseline 指标，跳过首轮测量

完整示例见 `templates/manifest.example.json`：模板按 AHE 论文 7 类
可编辑组件登记可变面，validation 用编译探针做候选校验。

运行级环境变量：

- baseline 复用：`AHE_REUSE_BASELINE`
- 费用记账：`AHE_COST_INPUT_PER_MTOK` / `AHE_COST_CACHED_PER_MTOK` /
  `AHE_COST_OUTPUT_PER_MTOK` / `AHE_COST_CURRENCY`
- 阈值保险：`AHE_KEEP_TARGET`（攒够 N 个 keep 收官）、`AHE_MAX_NOOP_STREAK`
  （连续 N 次失败熔断）

harness 专属行为通过 `register_adapter` 在引擎外注册，manifest 始终使用
`generic`/`command` 或自注册 adapter 名。

## Keep 门禁

默认门禁：主指标不降（generic 适配器为 `accuracy`）、token/时间不升、
f1 不倒退。可通过环境变量切换为战役门禁（按优先级从上到下）：

| 环境变量 | 规则 |
|---|---|
| `AHE_KEEP_ACCURACY_ONLY=1` | kernel 优化率严格 > 当前 best（单一目标） |
| `AHE_KEEP_TRI_METRIC=1` | 优化率↑ 且 迭代数↑ 且 token↓（三项同时） |
| `AHE_KEEP_EFFICIENCY_STRICT=1` | 迭代数↑ 或 token↓，且优化率 ≥ `AHE_EFFICIENCY_IMPR_FLOOR`（默认 5%） |
| `AHE_KEEP_PRODUCT_SCORE=1` | 主指标（乘积分）严格 > 当前 best |
| `AHE_KEEP_SPARSE_METRICS=1` | 迭代数与优化率都严格增加 |

所有门禁之前都有硬性前提：`runner_ok`（评测器自报有效，官方精度/verifier 通过）。

## 产物

- `trace/.../ahe-component-report.json`：baseline、每轮候选、keep/revert、全部指标
- `trace/.../trace.jsonl`：逐事件轨迹（评测、校验、round error）
- `trace/.../patch-report/`：harness/patches 风格的可审阅 patch 报告（见下节）
- `workspace/.../round-N-<hash>/`：被接受候选的完整快照；revert 的候选自动清理
- `changes/round-*.json`：结构化 patch 提案与应用结果

## 导出 patch 报告（harness/patches 风格）

campaign 结束时引擎自动在 `<trace目录>/patch-report/` 生成一份 GitCode 可
浏览的报告，结构与 `harness/patches/<类别>/<campaign>/` 一致：

```text
patch-report/
├── README.md                    # campaign 总览：使用方式 + 轮次表格 + 证据路径
├── 01-round-1-3a2e2365.patch    # 冻结基线 → 该轮候选的累计 diff
├── 01-round-1-3a2e2365.md       # 本轮修改内容 / 目的 / 观测结果
├── 02-round-3-9f6c484d.patch    # 基线 → round 3 的累计 diff（含此前全部 keep）
└── ...
```

每个 kept 候选均为"基线 + 此前全部 keep + 本轮新增"，因此每个 `.patch`
都是从同一冻结基线出发的**自包含累计 diff**——复现某一轮只需应用一个
patch，不应将 01..NN 依次连续应用。

- 定向输出（例如归档进仓库）：`AHE_PATCH_REPORT_DIR=/repo/harness/patches/pypto/<campaign>`
- 关闭自动生成：`AHE_PATCH_REPORT=0`
- 事后重导出：`python3 plugins/ahe/ahe.py --export-patch-report <trace>/ahe-component-report.json --target <目录>`

## 导出最终版 harness

优化完成后，可将最终被接受的候选导出为可直接使用的目录（源 harness 保持
不变）：

```bash
python3 plugins/ahe/ahe.py --export-best /path/to/trace/ahe-component-report.json \
  --target /path/to/final-harness
```

导出时输出 baseline 与 final 的指标对比及使用命令（Pi 插件注册、
Codex 斜杠命令安装、prompt 直接使用等，按 harness 类型自动
识别）；未指定 `--target` 时默认导出到 `<harness>.ahe-best`。

**默认自动安装**：`--export-best` 默认按 `<harness>-<任务名>` 规则为导出副本的注册命令改名
（如 `/my-tool` → `/my-tool-pypto`，任务名取自 `--eval-project`，与原 harness 共存），并将导出目录
注册进 Pi 的 packages——**重启 Pi 后即可使用新命令**：

```bash
python3 plugins/ahe/ahe.py --export-best <trace目录>/ahe-component-report.json \
  --target /path/to/my-tool-pypto
# 重启 Pi 后 /my-tool-pypto 可用

# 可选项: --as-suffix <任务名> 显式指定（默认从评测任务名自动推导）; --no-register 不注册; --no-rename 保留原命令名
```

**Codex 侧同样自动安装（插件方式）**：现代 Codex（≥0.14x）的斜杠命令来自
插件体系而非 prompts 目录。若 harness 按本插件约定携带
`assets/codex/prompts/*.md` 命令模板（占位符 `__AHE_PLUGIN_ROOT__` /
`__AHE_SCRIPT__`），导出时自动完成：

1. 渲染模板（占位符指向导出副本的绝对路径）写入导出目录
   `commands/<命令名>-<后缀>.md`——命令正文以 `$ARGUMENTS`/`$N` 接收
   用户参数（与官方 marketplace 插件同构）；
2. 生成 `.codex-plugin/plugin.json` 与单插件 marketplace manifest
   `.agents/plugins/marketplace.json`（插件名带后缀，与原 harness 共存）；
3. 调用官方 CLI 注册：`codex plugin marketplace add <导出目录>` +
   `codex plugin add <插件名>@<marketplace>`（缓存到
   `<CODEX_HOME>/plugins/cache/`）。

注册后插件名带 `-v2` 后缀（如 `my-tool-v2`），与原 harness 共存，可经
`$提及`/其 MCP server 使用；渲染后的命令模板同时写入
`<CODEX_HOME>/prompts/`，兼容仍读取该目录的旧版 codex。注意：插件
commands 的斜杠命令呈现由 codex 版本决定（0.166 实测未激活插件 commands
与 prompts 目录）。`CODEX_HOME` 默认 `~/.codex`，可用环境变量覆盖；CLI
不可用时导出仍写好全部插件文件并打印手动注册命令。自带
`.codex-plugin/plugin.json` 的 harness 即使无命令模板也会
以 `-v2` 插件名注册；两者皆无的 harness 不受影响：Codex 侧仍按 MCP 方式
接入（`bash <导出目录>/install.sh` 或运行时 `-c mcp_servers.*` 指向导出
目录）。

`--no-register` 同时跳过 Pi 与 Codex 的自动注册；`--as-suffix` 同时作用于
两侧命令名；`--no-rename` 保留原命令名（有与原 harness 冲突的风险）。

## 运行流程

```text
冻结 baseline（或 AHE_REUSE_BASELINE 复用）
    ↓
每轮：进化模型读 harness 上下文 + 失败名单 → 提结构化 patch（锚点 + hash 校验）
    ↓
候选落盘到隔离 workspace → validation（编译/导入）→ 真实评测器执行
    ↓
门禁判定 → keep（累积进 best）或 revert（进失败名单，驱动下一轮）
    ↓
累计 --rounds 个 keep 或触发熔断后收尾，输出报告与可审阅 patch
```

写回策略：默认 `--no-write-back` 语义适用于冻结类目标（永不写回源目录）；
通用 harness 可去掉该参数让最优候选覆盖源目录（改动前自动备份
`*.ahe-backup`）。

## 配套工具

| 工具 | 用途 |
|---|---|
| `skills/auto-harness-adapter/` | 自动发现任意 harness 的可变面（对齐 AHE 论文 7 类组件）并生成 manifest 草稿 |
| `skills/autoharness-convergence/` | 分析 AHE 运行历史，判断收敛类型并给出下一步干预 |
| `artifact_bridge.py` | 把 AHE 报告导出为可提交的 artifact bundle |

## 文件结构

```
plugins/ahe/
├── ahe.py                     # CLI 入口 + 兼容 facade（python3 ahe.py / import ahe）
├── component_mode.py          # 组件模式兼容 facade（from component_mode import ...）
├── ahe_engine/                # 引擎实现（模块图见 ahe_engine/__init__.py）
│   ├── api_config.py          #   LLM API 发现（env / env 文件 / Pi / Codex）
│   ├── llm_client.py          #   OpenAI 兼容 chat 客户端
│   ├── fsutil.py              #   共享文件系统小工具
│   ├── artifacts.py           #   报告 → Git artifact bundle
│   ├── single_mode.py         #   单文件 prompt 模式优化循环（模式 A）
│   ├── evaluator_gen.py       #   评估器 + AHE-OBJECTIVE 生成
│   ├── preflight.py           #   安装入口检测 / 安装 / 只读预检
│   ├── export.py              #   --export-best 最终版导出
│   ├── cli.py                 #   argparse 入口
│   └── component/             #   组件模式闭环：metrics / commands / context /
│                               #   runenv / debugger / patches / propose /
│                               #   evaluate / gates / loop（run_component_mode）
├── patch_report.py            # campaign 结束自动生成 harness/patches 风格报告
├── harness_optimizer/         # schema、workspace、runner、trace/evidence、discovery
├── install.py                 # 统一安装器：--pi 注册 Pi / --codex 装 Codex prompt
├── install.sh / uninstall.sh  # bash 包装（同参数：install.sh --pi/--codex）
├── start-pi-ahe.sh           # 未注册时的单次隔离拉起
├── skills/                   # auto-harness-adapter / autoharness-convergence
├── assets/pi/extensions/ahe.ts # Pi 扩展（/ahe 命令）
├── assets/codex/prompts/ahe.md # Codex 自定义 prompt 模板（/ahe 命令）
├── templates/                 # 最小 manifest / eval.py 模板
├── test/                      # 全部单元测试：python -m unittest discover -s test -t .
├── QUICKSTART.md              # 快速入门
└── README.md
```
