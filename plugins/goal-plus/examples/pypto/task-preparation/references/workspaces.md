# 工作区准备

从本 skill 目录执行脚本，先激活安装了匹配版本 PyPTO、simpler、Torch 和 golden harness 的环境。
路径参数按本机实际位置填写。输出使用仓库外的新目录；已有任务和实验数据不覆盖。

## 任务类型

| 类型 | 起点 | 验收 | 准备方式 |
| --- | --- | --- | --- |
| 生成 | Torch reference，空 PyPTO kernel | CPU simulator 正确性 | 提取 reference/input，编写 `contract.py`，复用 `verify.py` |
| 优化 | 固定版本的已有 PyPTO 实现 | 正确性与真实设备耗时 | 保留上游源码与依赖，根据其入口建立性能 verifier |

两者都固定来源、输入和验收合同，但不共用一个评分指标。CPU 检查通过不能替代 NPU baseline。

## 生成目录

```text
task/
|-- kernel.py          # 待实现入口
|-- config.py          # 签名常量和动态维度
|-- inputs.py          # 输入构造入口
|-- reference.py       # Torch golden 入口
|-- oracle/            # 实际用到的 Torch helper，保留各自命名空间
|-- contract.py        # case 参数、比较器和执行配置
|-- verify.py          # 通用正确性运行器
|-- provenance.json   # 源文件、版本、specialization 与 hash
`-- .agents/skills/pypto-kernel/SKILL.md
```

提取器保留 helper 的模块边界，避免不同模块的同名常量被混合；不复制上游 PyPTO kernel。
任务说明、prompt 和 LICENSE 也随目录交付。环境、数据及编译产物不打包进模板。

## 接入 Goal Plus

以生成的 `task/` 为 Search `source_path`，实现、verifier 和 Git 跟踪的
`.agents/skills/pypto-kernel/SKILL.md` 均位于其中；源码 checkout 与准备日志留在外层。
Verifier 命令、edit surface 和 task-local skill 路径相对此 source 定义，能随 candidate 搬迁。

在任务环境运行 `python -c 'import sys; print(sys.executable)'` 获取解释器绝对路径，
不要解析 venv 软链接。用该解释器检查 `-I -c 'import pypto, simpler, torch, golden'`；
冻结 verifier 不继承外部 `PYTHONPATH` 或用户 site-packages。所需依赖安装到任务环境，
为 verifier 明确配置必要的编译器、工具链和动态库路径，不向插件环境安装任务依赖。
在不依赖交互 shell 隐式配置的新进程中，使用上述环境先做导入和最小编译、执行检查，
通过后再运行完整输入与 reference 校验；小型检查不替代完整验收。

生成任务可将合法的编译或数值校验失败记为有限的 0 分，使空实现成为可搜索的起点；
评分包装层只转换经过校验的任务结果，并保留诊断。缺依赖、启动失败、进程异常退出或
缺少合法 JSON 结果应报错，不能兜底成 0 分；仅存在 `valid=false` 不足以判定为有效失败。
最终验收仍使用严格的正确性 gate。

冻结后核对 baseline 日志中的实际执行与预期一致，以及 `task_skill_paths` 包含 kernel skill，
再创建 run、派发 worker。工具返回 freeze 成功不替代 Main 对验收合同和环境的检查。

## 提取示例

在加载环境后执行下面命令，将标准输出作为 `apply_patch` 输入：

```bash
python scripts/extract_reference.py \
  --source /path/to/pypto-lib/models/deepseek_v4_flash_dspark/decode_csa.py \
  --source-url https://github.com/hw-native-sys/pypto-lib/blob/main/models/deepseek_v4_flash_dspark/decode_csa.py \
  --reference golden_decode_csa --inputs build_distributed_tensor_specs \
  --entry l3_decode_csa --specialize='--tp 2' \
  --license /path/to/matching/LICENSE \
  --output /path/to/new-task
```

MoE 对应 `deepseek_v4_flash_mtp/moe.py`、`golden_moe`、`build_tensor_specs`、`l3_moe`、
`--specialize='--ep 2'`。不同 case 的 `contract.py` 由实际上游比较逻辑决定，而不是套 MoE 的容差。

这里的 URL 记录请求来源，实际提取版本以 checkout commit 和文件 hash 为准。
CSA 与 MoE 的准备流程已在 pypto-lib `b5df050ef2a2e04706b722874f66eb955bd6b4e3` 上检查过；
该版本不代表 GitHub 最新 main。版本变化后重新检查签名、依赖及比较器。

CSA 原始 TP=2 检查七项：`x_out`、两个 compressor state、原始/压缩 KV cache、indexer KV
cache 与 scale。mapped pool 比较器在有效区域执行数值比较，同时要求未映射区域保持原值，
不能换成对整个物理池的普通 allclose。

编写 `contract.py` 并复制 verifier 模板后运行：

```bash
python scripts/check_reference.py \
  --task /path/to/new-task \
  --upstream /path/to/pypto-lib/models/deepseek_v4_flash_dspark/decode_csa.py
```

上例默认选择 `CASES["default"]`；其他配置通过 `--case` 逐项检查。
此版本的 `golden` 默认将 Torch 线程数设为 16，不只由 `OMP_NUM_THREADS` 决定。
CPU 资源有限时可在命令前加 `PYPTO_GOLDEN_NUM_THREADS=1`，并记录验收使用的线程设置。

任务内 kernel skill 通常链接 `language/00-types.md`、`language/01-functions.md`；涉及 host/rank
编排时再链接 `distributed/07-programming_model.md` 等。CPU 上设备编号代表模拟 rank，非真实 GPU。
