---
description: AHE harness 优化：eval任务为<benchmark路径>里面的<项目>, 或 manifest/adapter/真实 evaluator
argument-hint: <harness 路径或命令>, <任务描述>, <目标/指标>, N轮
---
<!-- ahe-plugin-root: __AHE_PLUGIN_ROOT__ -->

# AHE Harness 优化任务（Codex /ahe）

## 用户原始参数

```text
$ARGUMENTS
```

若上面为空或仍是字面量 `$ARGUMENTS`：不要执行任何命令，只回复用法
`/ahe <harness 路径或命令（如 /my-tool，自动识别）>, <任务描述>, <目标/指标>, N轮`，
并说明可选段：`eval任务为<benchmark>里面的<项目>`、`manifest路径为...`、`评测器为...`、
`holdout评测器为...`、`任务根目录为...`、`生成评估器`、`自动发现adapter`。

## 第 1 步：解析参数（在推理中完成，不写解析脚本）

把用户参数按中英文逗号或换行分段，逐段归位（显式段先于自由段，先命中先用）：

| 字段 | 识别规则 |
|---|---|
| harnessPath | `harness路径为X` / `路径为X`；或第一个路径形态段（`/a/b`、`./a`、`../a`；Windows 绝对路径 `D:\a\b`、`C:/a/b` 也算） |
| harnessRef | 第一个 `/<命令>` 形态段（如 `/my-tool`）：优化"提供该命令的插件"，需先定位其源码目录 |
| manifestPath | `manifest路径为X`；若 harness 段本身指向 `manifest.json`，则该文件为 manifestPath、其所在目录为 harnessPath |
| adapter | `adapter为X` / `适配器为X` |
| evalCommand | `评测器为X` / `eval-cmd为X`（以 `eval任务` 开头的段不算） |
| holdoutEvalCommand | `holdout评测器为X` / `holdout-eval-cmd为X` |
| taskRoot | `任务根目录为X` / `task-root为X` |
| evalTaskRoot / evalTaskProject | `eval任务为<benchmark路径>里面的<项目>`（无 `里面的` 时只填 root） |
| generateEvaluator | 段内含 `生成评估器` 或 `generate-evaluator` |
| discoverAdapter | 段内含 `自动发现adapter` 或 `discover-adapter` |
| outputManifest | `输出manifest为X` / `output-manifest为X` |
| task | `提示词为X` / `任务为X`；否则剩余自由文本（去掉已归位段与指标段）拼接 |
| tokenTarget / timeTarget / accuracyConstraint / rounds | `token降N%`、`时间降N%`（或 `e2e降N%`）、`准确率...`（缺省"不变"）、`N轮` |

harnessRef 定位：查 Pi 的 `~/.pi/agent/settings.json` packages、已注册扩展的源码位置，
或本机 Codex prompts（`~/.codex/prompts/`），找到提供该命令的插件源码目录作为 harness 根；
找不到就向用户要路径，不要猜。

## 第 2 步：定位引擎与 API 配置

- 引擎（绝对路径，安装时写入）：`python3 "__AHE_SCRIPT__"`
  （Windows 上 `python3` 不存在时用 `python`）
- 插件根目录：`__AHE_PLUGIN_ROOT__`
- evolve / eval / debugger 的 LLM API 自动回退顺序：环境变量 `LLM_*` → `AHE_ENV_FILE`
  → Pi 会话配置 → **Codex 本地配置（`~/.codex/auth.json` 的 OPENAI_API_KEY +
  `config.toml` 的 model / `model_providers.<id>.base_url`）** → 内置 env 文件。
  都没命中时提醒用户 `export LLM_BASE_URL / LLM_API_KEY / LLM_MODEL` 再重跑。

## 第 3 步：执行流程

0. 运行前预检（只读，不要做多轮冒烟）：

   ```bash
   python3 "__AHE_SCRIPT__" '<harness路径>' --preflight
   ```

   预检报告安装入口、依赖状态（node_modules/.venv/运行探针）和运行方式；未就绪就把
   `--preflight` 换成 `--install-harness`（入口优先级：manifest installation.command >
   install.sh > install.py > package.json > pyproject.toml）；然后直接
   `--baseline-only` 真实验证——baseline 本身就是唯一的
   冒烟，不要在它之前再跑任何扩展/链路冒烟。

然后按是否已有 manifest 分两种模式：

### 模式 A：manifest 已知（`manifest路径为...`，或 harness 目录里存在 harness.manifest.json / manifest.json）

1. 读取 manifest，按其中声明识别可编辑组件。修改范围对齐 AHE 论文
   （arXiv:2604.25850，NexAU 框架）的 7 类可编辑组件：system prompt、tool
   description、tool implementation（运行时）、middleware、skill、sub-agent 配置、
   long-term memory（ledger/handoff）；论文消融显示收益主要集中在 tool
   implementation、middleware 和 long-term memory，优先这些组件。
2. 按 manifest 指定的 adapter 装载并运行真实 harness；从
   evaluation.command/evaluation_command（或显式 评测器为...）取得真实 evaluator；
   validation.command 只用于候选校验；adapter/evaluator/runner 源码是可执行组件，
   不要把它们拼成 system prompt。
3. （仅当"生成评估器"且未提供 评测器为...）先根据任务生成评估器和 keep 主指标，
   生成后必须审阅再放行：

   ```bash
   python3 "__AHE_SCRIPT__" '<harness路径>' --task '<任务描述>' \
     [--eval-task '<benchmark根>' --eval-project '<项目>'] \
     --manifest '<manifest路径>' --generate-evaluator
   ```

   审阅要点：真实运行 harness、stdout 最后一行输出含主指标的 JSON、非 stub、
   不修改任务目录；AHE-OBJECTIVE 已写入 manifest，keep 门禁自动跟随。
4. 通过 AHE 引擎执行完整的基线、进化和真实 evaluator 回归：

   ```bash
   python3 "__AHE_SCRIPT__" '<harness路径>' \
     --manifest '<manifest路径>' --adapter '<adapter|auto>' \
     --task '<任务描述>' --rounds <N> \
     [--token-target 'N%'] [--time-target 'N%'] [--accuracy-constraint '不变'] \
     [--eval-cmd '<评测命令>'] [--holdout-eval-cmd '<holdout命令>'] \
     [--task-root '<任务根目录>'] \
     [--eval-task '<benchmark根>' --eval-project '<项目>']
   ```

5. 只在 manifest 声明的组件边界内修改，依据 evaluator 与 holdout evaluator 输出的
   score/F1、tokens、time 决定保留或回退（keep/revert 由引擎自动执行）；源目录冻结，
   只保留 candidate。
6. manifest.references 只作为修改 Agent 的只读参考材料，不能直接注入 harness
   runtime；evolve 允许并鼓励上网搜索相关 skill、中间件模式与相关论文（AHE 论文
   arXiv:2604.25850、NexAU、公开 coding-agent harness 等）作为修改参考——参考只出
   思想，patch 锚点必须来自当前组件文件。
7. 汇报真实 evaluator/holdout evaluator 的优化前后结果和实际修改组件。

### 模式 B：未给 manifest

1. 检查 `<harness>/harness.manifest.json` 与 `<harness>/manifest.json`，存在即切换到
   模式 A，不得把所有 .py/.ts 运行时代码当作 prompt。
2. 若用户要求 `自动发现adapter`：只执行 discovery 生成 manifest 草稿，不进入优化
   循环；审阅 selected、review、excluded 和 warnings（确认任务/评测器/golden 未进
   可变面，7 类组件该进的都进了）。
3. 未发现 manifest 时，**禁止直接手改 harness 源码做优化循环**；必须先 discovery：

   ```bash
   python3 "__AHE_SCRIPT__" '<harness路径>' --discover-adapter \
     --task '<任务描述>' \
     [--eval-task '<benchmark根>' --eval-project '<项目>'] \
     [--eval-cmd '<评测命令>'] \
     --validation-cmd "python -m compileall ." --output-manifest ./harness.manifest.json
   ```

   审阅后用 `--manifest ./harness.manifest.json` 进入组件模式优化循环——隔离候选、
   锚点 patch、门禁、自动 keep/revert 都由引擎管，改坏自动回滚。
4. 旧的手动编辑循环（改文件→重测→不降才保留）仅当 harness 是单一 prompt 文件且
   用户明确要求手动模式时才允许。
5. 汇报最终结果：优化前后的 accuracy / tokens / time 对比。

### eval 任务接线（evalTaskRoot 存在时）

- Eval 任务根目录只读（benchmark checkout），绝不进入可变组件。
- 阅读该 benchmark 的 README、runners/、scripts/，选择其真实 runner/scorer 作为
  evaluator 通过 --eval-cmd 传入 AHE；禁止自造简化评分。
- evaluator 收到环境变量 AHE_HARNESS_ROOT（候选 harness 目录）、
  AHE_EVAL_TASK_ROOT（任务根目录）、AHE_EVAL_TASK_PROJECT（目标项目）；评测对象
  是候选 harness，任务代码与 golden 数据只读。
- 若 benchmark 提供独立 holdout/验证集，通过 --holdout-eval-cmd 传入。

## 收尾

1. campaign 结束后引擎已自动在 `<trace目录>/patch-report/` 生成 harness/patches 风格
   的报告（README.md + 每轮累计 .patch/.md）；定向输出用 AHE_PATCH_REPORT_DIR 环境
   变量或事后重导出：

   ```bash
   python3 "__AHE_SCRIPT__" --export-patch-report <trace目录>/ahe-component-report.json --target <报告目录>
   ```

2. 导出最终版 harness（默认自动加 -v2 后缀并注册为 Pi 插件，重启 Pi 后即可用新命令）：

   ```bash
   python3 "__AHE_SCRIPT__" --export-best <trace目录>/ahe-component-report.json --target <导出目录>
   ```

把 patch 报告位置、导出命令打印的指标对比、使用方式和新命令转达给用户。

## 约束

- 禁止在 harness 中嵌入答案或匹配规则
- 禁止删除导致准确率下降的内容
- 禁止多轮冒烟：预检一次 + baseline-only 一次就是全部验证
- 优化必须走 manifest/组件模式（隔离候选+门禁+自动回滚）；未发现 manifest 时先
  --discover-adapter 生成，禁止直接编辑 harness 源码做优化（单一 prompt 文件且用户
  明确要求手动模式的除外）
- evaluator 必须是真实 manifest/adapter 配置的评测器；禁止用 API 可用性探测或字符串
  匹配冒充真实结果；不得把 validation.command 当作 evaluator
- 不得把 adapter、evaluator、runner 或工具实现源码作为 prompt 内容发送给模型
- eval 任务目录（含 projects、golden、runner、scorer）只读；不得修改任务代码或评分
  逻辑来凑指标
- 准确率约束含"不变"时：准确率绝对不能下降
