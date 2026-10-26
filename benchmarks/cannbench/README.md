# CANNBench: Goal Plus + Triton-Ascend

本目录把 CANN Bench 作为 Git submodule 第三方库引入，并在 Muyuan 侧注册两个扩展：

- `GoalPlusGenerator`：auto_pipeline 每加载一个算子 case，就为该算子创建一个 Goal Plus SearchSpec，运行候选生命周期并返回选中工作区。
- `GoalPlusToCannConverter`：校验并复制标准 `build.sh + setup.py + cann_bench/` submission。

上游源码保持独立，Muyuan 只记录 `.gitmodules` 和固定 gitlink；版本与初始化说明见 `THIRD_PARTY.md`。注册通过 `src/sitecustomize.py` 的显式环境开关继承到 auto_pipeline 的后台、监控与重试子进程。

## Codex 运行入口

让 Codex 协助规划、启动、监控或重试本目录的 CANN Bench + Goal Plus 任务时，使用项目内的 [`cannbench-goal-plus` skill](.agents/skills/cannbench-goal-plus/SKILL.md)。目录级 [`AGENTS.md`](AGENTS.md) 固定了 T/K/C、Pi RPC、NPU 验证与证据边界；运行 Skill 只调用本目录已有脚本和 CANN Bench 原生 auto_pipeline，不另建一套控制 CLI。

当前 auto_pipeline 集成是 **adapter mode**：`GoalPlusGenerator` 直接创建 Goal Plus SearchSpec，并以 `driver: pi-rpc` 启动 Pi coding-agent candidate。它不会执行字面量的用户级 `/goal-plus` prompt，也不会创建完整的 native Goal Plus goal record。若明确需要 `pi -p "/goal-plus ..."` 的 intake、triage、promotion 和终态流程，应使用 Goal Plus 自身的 Pi skill；这属于另一种执行模式，不能与 auto_pipeline campaign 的证据混用。

## 代码生成后端切换

在对应 adapter 已实现的前提下，Triton-Ascend 与 AscendC 的后端切换只发生在配置中；Generator、converter 注册、算子级批量调度、monitor/retry 和最终 CANN `kernel_eval` 边界保持不变。例如，完成 AscendC 适配后的配置入口应为：

```yaml
agent:
  type: goal-plus
  backend: ascend
  arch: ascend910b4
  framework: torch
  codegen_target: ascendc
  config_path: benchmarks/cannbench/configs/goal-plus-ascendc.npu.yaml
```

当前 master 只实现并验证了 `codegen_target: triton_ascend`。AscendC 配置分支尚未接入；现在直接把配置改成 `ascendc` 会被 `GoalPlusGenerator` 拒绝，因此上面的 AscendC 配置是目标接口，不代表当前已经可运行。后续适配应封装在 Muyuan 的 generator/converter 内部，不要求使用者改变 auto_pipeline 的运行命令，也不修改作为第三方 submodule 引入的 CANN Bench。

## 当前无 NPU 验收

当前机器可跑完整控制链，但不会导入或执行 `torch_npu`、Triton kernel、CANN `kernel_eval`：

```bash
git submodule update --init --recursive benchmarks/cannbench/third_party/cann-bench
uv sync --project benchmarks/cannbench --extra dev
PYTHON=benchmarks/cannbench/.venv/bin/python \
  benchmarks/cannbench/scripts/run_non_npu_smoke.sh
```

这个轻量环境只安装适配层、Goal Plus 与 CANN auto_pipeline 控制面所需依赖；不会安装第三方 CANN Bench `pyproject.toml` 中的 `torch_npu`。

实际经过：真实 CANN case 加载 → GeneratorInput → Goal Plus freeze/create/plan/start → 两个隔离 candidate → 静态 verifier → select/report → converter → BenchmarkPipeline eval 边界。最后一个 eval 边界只做 Python 语法检查，并在 JSON 中明确写入 `npu_executed=false` 和 `kernel_eval_executed=false`。

还可以让 CANN Bench 原生 CLI 承担多算子进程、状态、monitor 和 retry；这个入口通过独立环境开关替换且只替换最终 `kernel_eval`：

```bash
PYTHON=benchmarks/cannbench/.venv/bin/python \
  benchmarks/cannbench/scripts/run_auto_pipeline_non_npu.sh run \
  --config benchmarks/cannbench/configs/auto-pipeline-goal-plus-non-npu.yaml \
  --workspace . --devices 0,1 --parallel 2 --share-device \
  --output benchmarks/cannbench/runs/auto-pipeline-non-npu --foreground

PYTHON=benchmarks/cannbench/.venv/bin/python \
  benchmarks/cannbench/scripts/run_auto_pipeline_non_npu.sh monitor \
  --output benchmarks/cannbench/runs/auto-pipeline-non-npu --once --json

PYTHON=benchmarks/cannbench/.venv/bin/python \
  benchmarks/cannbench/scripts/run_auto_pipeline_non_npu.sh retry \
  --output benchmarks/cannbench/runs/auto-pipeline-non-npu \
  --task mish --devices 0 --parallel 1 --foreground
```

生产脚本 `run_auto_pipeline.sh` 不设置 `MUYUAN_CANNBENCH_NON_NPU_EVAL`，所以不会使用该替身。

## NPU 机运行

准备好 CANN、`torch_npu`、Triton-Ascend、Pi RPC 后，从 Muyuan 根目录运行：

```bash
benchmarks/cannbench/scripts/run_auto_pipeline.sh run \
  --config benchmarks/cannbench/configs/auto-pipeline-goal-plus-mish.yaml \
  --workspace . \
  --devices 0 \
  --parallel 1 \
  --output benchmarks/cannbench/runs/mish-npu \
  --foreground
```

去掉 `--foreground` 后仍使用 CANN Bench 原生后台/monitor/retry；子进程从环境继承 Muyuan 注册钩子。外层 auto_pipeline 的 `--parallel` 负责不同算子任务的并发和设备分配；内层 Goal Plus 的 `goal_plus.max_parallel` 负责同一算子的候选搜索，生产配置默认 `K=4`。Generator 会为该算子打开一个 Goal Plus durable Pi pool，让 4 个 Pi coding-agent session 在独立 candidate workspace 中并发探索。它们调用 CANN verifier 时仍共享该算子被分配到的 NPU，并由冻结 SearchSpec 中的 `npu:<device_id>` 资源锁串行验证，所以 `K=4` 不等于需要 4 张卡。

例如 `--parallel 1 --devices 0` 配合 `goal_plus.max_parallel: 4`，表示 `C=1`（同时一个算子、占用 device 0）和 `K=4`（该算子内部四个候选并发搜索）。旧的 `goal_plus.max_candidates` 仍作为兼容字段读取，但新配置统一写 `max_parallel`。

生产模式下，Goal Plus 每个候选都会调用 CANN Bench verifier 获取 `overall_score`，最终选中 submission 还会再进入外层 auto_pipeline 的标准 `kernel_eval`。这两层真机验证在当前无 NPU 环境中尚未执行。

```text
cannbench/
├── configs/                 # Goal Plus 与 auto_pipeline 配置
├── scripts/                 # smoke 和原生 auto_pipeline 入口
├── src/muyuan_cannbench/    # generator、converter、注册与无 NPU client
├── tests/
├── third_party/cann-bench/  # Git submodule 第三方库
├── experiments/
└── reports/
```
