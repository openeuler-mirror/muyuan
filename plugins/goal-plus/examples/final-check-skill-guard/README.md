# Final Check Skill Guard

这个只依赖 Python 标准库的示例展示一次真实的
`$goal-plus-with-final-check` 或 `/goal-plus-with-final-check` 打回和重试。
公开 Search verifier 只对吞吐量排序；Git 跟踪的 task-local Skill 还要求优化不能依赖
输入对象地址、过期输出或其他 benchmark 捷径，由独立 FinalChecker 执行最终验收。

## 准备

从 Goal Plus plugin 根目录运行：

```bash
python3 examples/final-check-skill-guard/prepare_workspace.py
cd .tmp/final-check-skill-guard-workspace
```

准备脚本不会覆盖已有目录。需要重新运行时，用 `--output` 指定一个新的相对或绝对
路径。生成目录是独立 Git 仓库，包含两个可直接粘贴的 prompt。

初始实现故意按输入对象地址缓存完整结果。公开性能 verifier 会接受它，但同一输入
对象发生变化后，task-local Skill 检查会拒绝它：

```bash
python3 .goal-plus-verifiers/score_throughput.py
python3 .agents/skills/preserve-score-semantics/scripts/check_score_semantics.py
```

第一条命令应成功；第二条应报告 `stale_result_after_input_mutation` 和
`input_object_identity_shortcut`，并以状态码 1 退出。

## 运行

在生成的 workspace 根目录启动 Codex 或 Pi，再完整粘贴对应 prompt：

```bash
codex -c model_reasoning_effort=max
cat codex-goal-prompt.md
```

```bash
pi
cat pi-goal-prompt.md
```

prompt 不固定模型，只使用一个原生 candidate lane，因为该用例验证的是 FinalChecker
围栏，不是并行搜索质量。

## 预期结果

第一次 FinalChecker 查看尚未修复的源码，必须依据受 Git 跟踪的
`preserve-score-semantics` Skill 返回 fail，Goal 保持 active。Main 修复源码并运行
Skill 自带检查后，再申请一个具有新 `check_id` 的 FinalChecker；第二次检查通过后，
Goal 才变为 complete。

```text
Search 性能候选通过
        |
        v
FinalChecker #1 -- Skill 失败 --> Goal active --> Main 修复
                                                |
                                                v
                                    FinalChecker #2 通过
                                                |
                                                v
                                          Goal complete
```

可以从 plugin checkout 查看持久化结果：

```bash
./scripts/monitor_goal_plus.sh --once .tmp/final-check-skill-guard-workspace
```

Goal 记录应包含 `failed`、`passed` 两次检查。模板还包含一个故意无关的
`plugins/goal-plus/skills/unrelated/SKILL.md`；它不是 `.agents/skills` 下的 task Skill，
不得进入 FinalChecker 的验收规则清单。
