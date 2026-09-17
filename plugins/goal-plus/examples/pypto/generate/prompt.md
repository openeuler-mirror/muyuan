# Codex

在仓库外的独立运行目录启动 Codex，将 `<task-preparation 绝对路径>` 替换为
[准备 skill 目录](../task-preparation/SKILL.md) 的实际路径，再粘贴完整任务块。

```text
$goal-plus max_parallel=3 workspace_provider=git_worktree promotion_mode=artifact_only strategy=agent_guided

先阅读 <task-preparation 绝对路径>/SKILL.md，由 Main 按生成流程准备任务和验收合同。
将 https://github.com/hw-native-sys/pypto-lib.git 拉到 third_party/pypto-lib，固定并核对 commit b5df050ef2a2e04706b722874f66eb955bd6b4e3；已有干净且版本一致的 checkout 则复用，不覆盖已有修改。
以 models/deepseek_v4_flash_mtp/moe.py 的 Torch reference 和输入为依据，在 ~/pypto-moe-generation 构造独立生成任务，并从空 kernel 实现等价的 PyPTO MoE。保持 EP 功能、输入输出和精度标准不变，不导入上游 PyPTO 答案作为实现。
准备校验完成后，在同一个 Goal 中继续生成和验收；实现 worker 使用任务目录中的 kernel skill。
使用 CPU simulator 验证编译与正确性，最终沿用准备阶段确定的全部输入、输出和精度合同，不将模拟耗时作为真实设备性能收益。不设总时限、3 并发，完成后报告实现位置、验证结果和未验证项。
执行节奏：有方案后及时提交 verifier 验证，切勿长时间内部迭代而不提交方案。
```
