---
name: cannbench-goal-plus
description: 在 Muyuan 的 benchmarks/cannbench 中规划、启动、监控、终止或重试 CANN Bench + Goal Plus auto_pipeline 运行。用户要求 Codex 协助运行 CANNBench、选择 Triton-Ascend 或 AscendC 后端、配置 Pi RPC worker 的时长/并发、查看 campaign 或内部 Search 进展、恢复失败算子时使用。
---

# CANNBench Goal Plus 运行

把 Codex 当作只负责控制面的 operator：冻结运行参数、调用现有 CANN
auto_pipeline、监控外层算子任务和内层 Goal Plus Search，并保留可复核证据。
不要让 Codex 自己成为候选 kernel worker。

## 先确认执行模式

标准单 Mish K=4 请求以本 Skill 和 `benchmarks/cannbench/AGENTS.md` 为权威
runbook；只对下方明确列出的 YAML 和环境做 preflight，不要重新追踪 Python 源码、
registry、converter 或三方实现。只有非标准 profile 才补读 `README.md`；只有修改
submodule 边界才读 `THIRD_PARTY.md`。

默认使用 **auto_pipeline adapter mode**。这个模式由 `GoalPlusGenerator` 直接创建
SearchSpec，并用 `driver: pi-rpc` 启动 Pi coding-agent workers；它不需要也不会执行
字面量 `/goal-plus` 用户 prompt。

只有用户明确要求 **native Pi `/goal-plus` mode** 时，才读取并遵循：

```text
plugins/goal-plus/.pi/skills/goal-plus/SKILL.md
plugins/goal-plus/docs/pi.md
```

该模式必须由 `pi -p "/goal-plus ..."` 进入，并在 prompt 中明确目标、T、K、设备、
正确性约束和完成条件。不要把 native Pi 运行冒充 auto_pipeline campaign，也不要把
adapter Search 冒充完整 `/goal-plus` goal lifecycle。

## 标准单算子 K=4 Profile

用户要求“部署 Goal Plus + CANN Bench，启动 K=4，只跑一个算子”且未指定其他算子时，
直接使用下面的标准 profile，不要重新搜索 Generator、registry、converter 或三方源码：

```text
outer config=benchmarks/cannbench/configs/auto-pipeline-goal-plus-mish.yaml
inner config=benchmarks/cannbench/configs/goal-plus-triton-ascend.npu.yaml
task=tasks/level1/mish
backend=triton_ascend
T=3600 秒/候选（最大值）
K=4（Goal Plus 内部 Pi pool）
C=1（auto_pipeline 外层算子并发）
devices=0
gen_timeout=10800
eval_timeout=10800
```

从 Muyuan 根目录直接部署：

```bash
git submodule update --init --recursive \
  benchmarks/cannbench/third_party/cann-bench
uv sync --project benchmarks/cannbench --extra dev
```

完成启动前检查后，直接启动后台 campaign：

```bash
PYTHON=benchmarks/cannbench/.venv/bin/python \
  benchmarks/cannbench/scripts/run_auto_pipeline.sh run \
  --config benchmarks/cannbench/configs/auto-pipeline-goal-plus-mish.yaml \
  --workspace . \
  --devices 0 \
  --parallel 1 \
  --gen-timeout 10800 \
  --eval-timeout 10800 \
  --output benchmarks/cannbench/runs/<new-campaign>
```

不要添加 `--share-device`，也不要添加 `--foreground`；由 CANN Bench 原生后台进程
承担运行、monitor 和 retry。`K=4` 来自 inner config 的
`goal_plus.max_parallel: 4`，`--parallel 1` 只表示 `C=1`，二者不得互换。

若用户改变 task、T、K、backend 或设备策略，再在
`runs/<campaign>/control/` 中创建一次性配置副本；标准 profile 不需要生成新配置。

## 识别用户意图

如果用户只询问状态，直接进入“监控”；不要创建新 campaign。需要启动时，解析：

- tasks：CANN Bench task 路径或算子名；
- backend：当前可运行值只能是 `triton_ascend`；
- T：每个 Pi worker 的最大探索秒数；
- K：单算子内部 worker 数；
- C：auto_pipeline 同时运行的算子 task 数；
- devices：NPU ids；
- output：新的、位于 `benchmarks/cannbench/runs/` 下的目录。

“并发 N”含义不明确时，分别确认 K 和 C。K 由 Goal Plus 的 durable Pi pool 管理，
通过 `goal_plus.max_parallel` 设置，默认使用 `K=4`；K 个 coding-agent session 并行
探索同一个算子的独立 candidate workspace。它们的 CANN verifier 通过
`npu:<device_id>` 锁串行使用该算子被分配到的设备，因此不要把 K 当成设备数。
`C` 通过 auto_pipeline `--parallel` 设置，且不得大于独占 device 数，除非用户明确
要求并接受 `--share-device`。

## 启动前检查

从 Muyuan 根目录执行只读检查：

```bash
git submodule status benchmarks/cannbench/third_party/cann-bench
command -v pi
PYTHON=benchmarks/cannbench/.venv/bin/python \
  benchmarks/cannbench/scripts/run_auto_pipeline.sh list --json
```

在 NPU 主机上还要验证当前 shell 的 CANN、`torch_npu` 和设备可见性；使用主机已有的
环境加载约定，不在 Skill 中硬编码 conda、驱动或私有路径。确认配置满足：

```yaml
goal_plus:
  driver: pi-rpc
  verifier: cann
  max_parallel: <K>
  worker_budget:
    max_runtime_seconds: <T>
```

`verify_timeout` 是一次 verifier 的超时，不是 T；外层 `--gen-timeout` 必须覆盖 worker
探索、验证和 closeout，`--eval-timeout` 必须覆盖最终 CANN `kernel_eval`。生产运行不得
使用 `.smoke.yaml`、`run_auto_pipeline_non_npu.sh` 或
`MUYUAN_CANNBENCH_NON_NPU_EVAL`。

启动真实 NPU 工作前，向用户展示并确认：

```text
mode=auto_pipeline adapter
host=pi-rpc
backend=<codegen_target>
tasks=<task list>
T=<秒数>：每个 candidate worker 的最大探索时间
K=<数量>：一个算子 Search 内由 Goal Plus Pi pool 并发的 candidate worker 数
C=<数量>：同时运行的不同算子 task 数
devices=<ids>：默认独占
output=<绝对路径>
```

## 启动非标准 Profile

仅当 task、T、K、backend 或 device 策略偏离标准 profile 时，才在忽略的
`runs/<campaign>/control/` 中生成配置副本；不要修改共享的已提交生产 YAML。然后运行：

```bash
PYTHON=benchmarks/cannbench/.venv/bin/python \
  benchmarks/cannbench/scripts/run_auto_pipeline.sh run \
  --config <run-specific-auto-pipeline.yaml> \
  --workspace . \
  --devices <ids> \
  --parallel <C> \
  --gen-timeout <seconds> \
  --eval-timeout <seconds> \
  --output <output>
```

长任务使用 CANN Bench 自带后台模式；不要自行拼 `nohup`、`&` 或另一个 supervisor。
返回 run id、output、T/K/C、devices 和下一条监控命令。

## 监控

先读取外层状态：

```bash
PYTHON=benchmarks/cannbench/.venv/bin/python \
  benchmarks/cannbench/scripts/run_auto_pipeline.sh monitor \
  --output <output> --once --json
```

若某个算子已经创建 `work/goal-plus-attempts/<attempt>/state/`，从其中的
`runs/<run_id>/run.json` 获取 run id，再读取内层状态：

```bash
benchmarks/cannbench/.venv/bin/goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root <attempt-state-root> \
  --args-json '{"run_id":"<run_id>","stale_after_seconds":600}' \
  --pretty
```

每次状态答复分别报告：

- outer auto_pipeline：运行中/成功/失败/终止、已完成 task 数；
- inner Goal Plus Search：候选、Pi session liveness、verifier 次数、best score；
- final CANN eval：是否执行、原始 score、`npu_executed`；
- artifact：submission、Goal Plus report、batch/task result 路径。

任务仍运行时继续监控，不因为一次无输出就重启。终态后检查
`batch_result.json`、每个 task 的 `benchmark_result.json`、submission 和
`goal_plus_selection.json`，缺少 NPU 或 verifier 证据时标记为 partial。

## 重试与终止

只重试明确失败的 task，并保留原输出：

```bash
PYTHON=benchmarks/cannbench/.venv/bin/python \
  benchmarks/cannbench/scripts/run_auto_pipeline.sh retry \
  --output <output> --task <task> --devices <ids> --parallel <C>
```

只有用户明确要求停止时才调用：

```bash
PYTHON=benchmarks/cannbench/.venv/bin/python \
  benchmarks/cannbench/scripts/run_auto_pipeline.sh kill --output <output>
```

不得删除 campaign、candidate workspace、`.gp`/state 或报告来“重新开始”。
