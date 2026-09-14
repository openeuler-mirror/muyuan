---
name: pypto-task-preparation
description: Guide the Main Agent in preparing a PyPTO generation or optimization workspace from a pypto-lib file or URL, preserving the upstream validation contract and providing task-local kernel guidance for implementation workers.
---

# PyPTO Task Preparation

供 Main Agent 在任务准备阶段读取，将用户指定的 pypto-lib 算子构造成仓库外的独立任务目录。
生成任务从空 kernel 开始，优化任务保留已有 PyPTO 实现作为 baseline。
目录合同和命令示例见 [工作区准备](references/workspaces.md)。

用户目标包含实现或优化时，准备完成后在当前 Goal 中继续，不另起 Goal；只要求准备时交付后停止。
是否进入 Search 由 Main 按现有 Goal Plus 合同判断，准备完成不等于空 kernel 已具备可冻结的 baseline。
实现 worker 使用任务目录中的 kernel skill，不承担本 skill 的上游提取和验收合同构造。

## 共同准备

1. 把 URL 定位到明确的 checkout、commit 和文件，检查该文件及实际依赖。已有可用环境优先复用；
   不更新共享 checkout、不修改他人环境。实际路径和设备配置记录在生成的任务目录，不写入本 skill。
2. 从上游测试入口识别 kernel 签名、Torch golden、输入构造、编译平台、rank 数及全部比较器。
   特别检查 `Out` / `InOut`、cache/state、动态维度和 import 时的 TP/EP specialization。
   不自行缩小模型或放宽精度；CPU simulator 的耗时不能作为 NPU 性能。
3. 明确验收平台。CPU simulator 只验证编译与正确性；性能优化需真实设备、固定计时口径和 baseline。
   从可用任务环境获取 `sys.executable`，保留 venv 路径；准备检查、process 和 promotion verifier
   均使用这个 Python 的绝对路径。Goal Plus 会将裸 `python` / `python3` 绑定到插件解释器，
   不代表已激活的任务环境。记录工具链与依赖版本，在 verifier 实际环境中先完成依赖导入和
   最小编译、执行检查，再构造完整输入；具体要求见[接入 Goal Plus](references/workspaces.md#接入-goal-plus)。

## 生成任务

1. 核对 checkout/commit、reference/input/entry、specialization 和许可证实际路径后，阅读并运行
   [extract_reference.py](scripts/extract_reference.py)，提取函数依赖、空 kernel 签名和来源清单。
   脚本导入可信 checkout，输出 `apply_patch` payload；同一配置复用该 payload 完成审阅和应用，
   不为计数或重复查看重新提取。它不自动抓取网络代码或覆盖已有目录，只支持普通 Python
   reference；不支持的依赖做最小任务级处理，不扩展成任意 Python 转换框架。
2. 根据上游合同编写 `contract.py`：提供 `ENTRY`、`CASES`、`RUN_CONFIG`、`reference`、
   `make_specs(case)`、`make_comparisons(case)`、`RTOL` 和 `ATOL`。每个 case 用 `seed` 固定随机性，
   用 `inputs` 保存传给上游 input builder 的关键字参数。复用 [verify.py](assets/verify.py)，比较器需覆盖
   所有应验证的输出及可变状态。输入、reference、verifier 与 candidate 的实现分开。
3. 每个验收配置默认运行一次完整 [check_reference.py](scripts/check_reference.py)，覆盖输入与
   Torch reference bitwise 对齐、错误和缺失输出拒绝，以及上游 PyPTO 实现通过新 verifier。
   `--reference-only` 仅用于单独排查 reference，不作为完整校验的必经前置步骤。
   另运行任务 `verify.py` 确认空实现失败。上游答案只在准备检查进程中使用，不写入交付 workspace。

## 优化任务

保留固定版本的上游实现及必要依赖，不运行空 kernel 提取步骤。沿用原 golden 和输入，
根据上游运行入口构造 verifier，在同一合同下验证 baseline、候选和最终结果。
共用设备时串行测量，记录各 rank 的有效设备时间；主指标以用户选定的任务合同为准。
`assets/verify.py` 是 CPU 正确性模板，不能直接作为 NPU 性能评分器。

## 验收与交付

写清任务的 `README.md`、`prompt.md`、`AGENTS.md`：入口、环境、允许实现的位置、输入输出和
验收范围。将简短的 kernel guidance 放到任务的 `.agents/skills/pypto-kernel/SKILL.md`，并在
AGENTS 中指向它。进入 Search 时以任务目录作为 `source_path`，将 skill 纳入 Git，核对 freeze
返回的 `task_skill_paths`；不依赖外层目录的递归发现。指引只链接当前版本的相关语言文档，
不整批注入文档，不复制 MoE 特有的 rank 假设到别的任务。

需要 Search 评分包装时，按[接入 Goal Plus](references/workspaces.md#接入-goal-plus)区分
有效失败分数与基础设施错误，并核对 runtime 的实际 baseline 日志。

空实现失败是生成任务的正常起点，不要将 compile/环境错误伪装成通过。交付时区分“输入/oracle
已对齐”“上游解通过”“candidate 尚待生成”或“优化 baseline 已测”；补充配置未通过就明确
记录，不能静默删掉既定合同。

保留源代码版权头和相应 LICENSE，记录实际 pin、配置、验收结果及尚未覆盖的条件。
模型、并发和时间由外层任务决定，不属于 preparation skill 的默认约束。
