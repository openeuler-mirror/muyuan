# PyPTO DeepSeek 性能优化

两个独立的性能优化 case，每次选一个运行，2 个并发候选。
总时限以所选 prompt 为准：Pi CSA 不设任务总时限，其余任务块为一小时。
不设总时限不影响单次命令、verifier 和 worker dispatch 的有界超时。

| Case | 目标文件 | 背景 |
| --- | --- | --- |
| CSA | `models/deepseek_v4_flash_dspark/decode_csa.py` | 人工已有约 20% 的优化，仅作背景，不是本例实测结果 |
| MoE | `models/deepseek_v4_flash_mtp/moe.py` | 近期较难取得明显优化 |

## 源码与环境

本例只保存说明和 prompt，不内置源码，也没有准备脚本或预制 SearchSpec。
Main 在运行目录的 `third_party/pypto-lib/` 获取
[hw-native-sys/pypto-lib](https://github.com/hw-native-sys/pypto-lib)，固定到
`b5df050ef2a2e04706b722874f66eb955bd6b4e3`，核对 HEAD 后作为 baseline。
已有干净且版本一致的 checkout 可以复用；否则使用新目录，不覆盖已有修改。
第三方源码、环境配置和运行产物不提交到本例。
本地 Git worktree 若因上游 `.agents/skills` 符号链接而拒绝冻结，可将其等价展开为普通目录；
将这类环境适配与 verifier 单独记录，不把它们算作模型优化。

目标机器需要可用的 PyPTO、simpler、PTOAS、CANN 和两张可直接使用的卡。
工具链安装见上游的
[安装说明](https://github.com/hw-native-sys/pypto-lib/blob/b5df050ef2a2e04706b722874f66eb955bd6b4e3/docs/get-started/installation.md)。
PyPTO 本身的版本需与 case 兼容；按该 PyPTO 版本固定其配套依赖，并记录实际版本。
安装环境建议在开始任务前完成。

冻结 verifier 时，命令使用已配置 PyPTO 环境的 Python 绝对路径，可在该环境中通过
`python -c 'import sys; print(sys.executable)'` 获取，保留虚拟环境路径，不解析解释器软链接。
裸 `python` / `python3` 会绑定 Goal Plus 自带解释器，不代表宿主 shell 的 PyPTO 环境。
Verifier 不继承外部 `PYTHONPATH` 或用户 site-packages；PyPTO 与 simpler 须安装在所选环境中，
并提前确认 `python -I -c 'import pypto, simpler, torch'` 成功。

启动宿主前配置好工具链环境、模型和两张可用卡；机器路径和设备分配属于本地运行配置，
不临时追加到任务 prompt。若设置 `ASCEND_RT_VISIBLE_DEVICES` 选择两张物理卡，
验收入口的 `-d` 应使用重新编号后的逻辑卡 `0,1`，不能再次传物理卡号。
Main、worker 和 verifier 沿用启动宿主时配置的同一对卡，不自行覆盖设备可见性。
TT 内也必须能访问工具链、设备及可读写的 POSIX 共享内存
（`/dev/shm`），供 PyTorch `share_memory_()` 使用；仅在宿主 shell 中可用还不够。

`PYPTO_BENCH=1` 启用上游 `golden.run` 的设备计时，不是额外安装的 CLI。
Main 依据原版验收入口建立评价合同，先测 baseline，再冻结并开始搜索。
验收脚本将 `PYPTO_PROG_BUILD_DIR` 指向每次调用的 `GOAL_PLUS_VERIFIER_TMPDIR` 下，
不在源码或候选目录生成 `build_output/`；Git 忽略编译产物并不代表候选 verifier 可写源码目录。
在原 CLI 运行前固定 `torch.manual_seed(20260909)` 以便比较，保留原版精度阈值，
不通过换 seed 筛掉失败样本。验收失败必须返回非零；使用 `tee` 时开启 `pipefail`。
允许修改相关 PyPTO 实现及依赖，不限函数体；输入、功能、golden 和精度标准保持不变。
主指标为各 rank 的平均 `effective` 设备耗时中的最小值，同时记录各卡耗时。
上游计时日志的 `rank` 标签可能是进程 ID；确认两个不同标签及有限数值，不要求标签为 `0,1`。
2 并发指候选探索；共用设备时串行测量，避免互相干扰。若 prompt 设置了总时限，
源码获取、编译、验证和收尾均计入该预算；未完成的验证如实报告。
golden、验收脚本和计时逻辑保持只读。

## 运行

先将本例的说明和对应 prompt 复制到仓库外的独立运行目录。每个 case、每种宿主分别使用一个目录，
然后在该目录启动宿主；不需要提前手动拉取第三方源码。
测试时原样提交下方链接中的完整任务块。任务要求需要调整时，先修改对应 prompt 文件，
再使用更新后的文件测试，不维护另一份临时启动 prompt。

ThinkThread + Pi：

```bash
tt run --profile goal-plus-pi --fs "$PWD" --write /dev/shm
```

使用已安装 Goal Plus 的 Profile，并确认其可以访问所需工具链、网络和设备。
开始前分别检查 Root 与 `fs.run(writes=discard)` 的共享内存访问；Root 检查通过不代表
快照 verifier 可用。`--write` 的外部路径对 Child 仍为只读，不能据此假定 Child 可直接验收。
在 Root TUI 中粘贴 [TT prompt](tt-goal-prompt.md) 的一个完整任务块。

原生 Pi（不使用 ThinkThread）：

```bash
pi
```

使用已安装 Goal Plus 的 Pi，粘贴 [Pi prompt](pi-goal-prompt.md) 的一个完整任务块。
本地 verifier 直接使用宿主工具链和设备，不经过 TT 快照执行。

原生 Codex：

```bash
codex
```

使用已安装 Goal Plus 的 Codex，粘贴 [Codex prompt](codex-goal-prompt.md) 的一个完整任务块。
原生 Pi/Codex 使用本地 Git worktree，TT 使用 ThinkThread；均保留 artifact-only 结果，
不回写原始 baseline。使用当前宿主所选模型。

运行结束查看 Goal Plus 给出的报告与 artifact 路径，核对 baseline、最佳快卡耗时、
各卡耗时、精度结果和未验证项。TT 的记录位于 Pi session-private Goal Plus root；
原生 Pi/Codex 的记录位于运行目录的 `.gp/`。
