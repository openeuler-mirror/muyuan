# Codex

每次只粘贴一个任务块，在独立运行目录的 Codex 会话中执行。

## CSA

```text
$goal-plus max_duration_seconds=3600 max_parallel=2 workspace_provider=git_worktree promotion_mode=artifact_only strategy=agent_guided

先阅读当前目录 README.md，按其中的环境与验收约定执行。
将 https://github.com/hw-native-sys/pypto-lib.git 拉到 third_party/pypto-lib，固定并核对 commit b5df050ef2a2e04706b722874f66eb955bd6b4e3 作为 baseline；已有干净且版本一致的 checkout 则复用，不覆盖已有修改。
帮我优化该仓库 models/deepseek_v4_flash_dspark/decode_csa.py 性能，保持 TP 功能不变和精度不退化。相关 PyPTO 实现及依赖均可修改，不限函数体，验收标准不变。
使用上游 PYPTO_BENCH=1 在两张可用卡上直接验收，主指标取快卡耗时，同时记录各卡耗时；共用卡时串行测量。先测 baseline，再优化；总预算一小时、2 并发，完成后报告结果和未验证项。
```

## MoE

```text
$goal-plus max_duration_seconds=3600 max_parallel=2 workspace_provider=git_worktree promotion_mode=artifact_only strategy=agent_guided

先阅读当前目录 README.md，按其中的环境与验收约定执行。
将 https://github.com/hw-native-sys/pypto-lib.git 拉到 third_party/pypto-lib，固定并核对 commit b5df050ef2a2e04706b722874f66eb955bd6b4e3 作为 baseline；已有干净且版本一致的 checkout 则复用，不覆盖已有修改。
帮我优化该仓库 models/deepseek_v4_flash_mtp/moe.py 性能，保持 EP 功能不变和精度不退化。相关 PyPTO 实现及依赖均可修改，不限函数体，验收标准不变。
使用上游 PYPTO_BENCH=1 在两张可用卡上直接验收，主指标取快卡耗时，同时记录各卡耗时；共用卡时串行测量。先测 baseline，再优化；总预算一小时、2 并发，完成后报告结果和未验证项。
```
