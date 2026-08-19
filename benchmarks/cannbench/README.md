# CANNBench: Goal Plus + Triton-Ascend

本目录把 CANN Bench 作为 Git submodule 第三方库引入，并在 Muyuan 侧注册两个扩展：

- `GoalPlusGenerator`：auto_pipeline 每加载一个算子 case，就为该算子创建一个 Goal Plus SearchSpec，运行候选生命周期并返回选中工作区。
- `GoalPlusToCannConverter`：校验并复制标准 `build.sh + setup.py + cann_bench/` submission。

上游源码保持独立，Muyuan 只记录 `.gitmodules` 和固定 gitlink；版本与初始化说明见 `THIRD_PARTY.md`。注册通过 `src/sitecustomize.py` 的显式环境开关继承到 auto_pipeline 的后台、监控与重试子进程。

## 代码生成后端切换

对 auto_pipeline 使用者，Triton-Ascend 与 AscendC 的预期用法都是只切换配置；Generator、converter 注册、算子级批量调度、monitor/retry 和最终 CANN `kernel_eval` 边界保持不变。例如，完成 AscendC 适配后的配置入口应为：

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

去掉 `--foreground` 后仍使用 CANN Bench 原生后台/monitor/retry；子进程从环境继承 Muyuan 注册钩子。外层 auto_pipeline 负责算子级任务并发与设备分配，内层 Goal Plus 的 `max_candidates` 默认是 1，内部 CANN verifier 同时声明 `npu:<device_id>` 资源锁。

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
