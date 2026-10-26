# Circle Packing：Git 工作区回写

这个示例沿用 [Circle Packing](../circle-packing/README.md) 的优化目标、
verifier、两个候选通道和五分钟运行约定，专门展示完整的可写 Git 流程：
创建真实的候选 worktree、选择 verifier 验证过的最佳提交，再把结果应用回
原始源码仓库。

仓库中受版本控制的 Circle Packing 模板不会被修改。准备脚本会将模板复制到
被忽略的 `workspace/` 目录，并把它初始化为独立 Git 仓库。Goal Plus 随后可以
使用 `promotion_mode=apply`，而不会改动示例自身的受控文件。

> [!IMPORTANT]
> 从本示例目录 `circle-packing-git-apply/` 启动 Codex 或 Pi。
> `search-spec.json` 的 `source_path="workspace"` 相对该目录解析；源码仓库位于
> `workspace/`，Goal Plus 运行状态位于并列的 `.gp/`。

## 准备可写源码仓库

在本示例目录中运行：

```bash
cd examples/circle-packing-git-apply
python3 prepare_workspace.py
```

命令会创建下面这个干净的源码仓库：

```text
workspace/
|-- .git/
|-- .gitignore
|-- .goal-plus-verifiers/circle_packing_score.py
|-- README.md
`-- initial_program.py
```

如果 `workspace/` 已存在，脚本会拒绝覆盖。需要丢弃之前生成的示例工作区并重新
开始时，使用：

```bash
python3 prepare_workspace.py --reset
```

`--reset` 只会删除 `.git/` 中带有本示例专用标记的工作区，不会删除任意同名
目录。上一轮运行可能已把最佳候选回写进 `workspace/`，使 baseline 高于初始的
`1.79835`；重新验证流程前先执行 `--reset`，避免把已优化的 baseline 当作起点。

准备完成后，目录层级应当是：

```text
circle-packing-git-apply/
|-- search-spec.json
|-- workspace/             # 待优化的干净 Git 仓库
`-- .gp/                   # 启动 Goal Plus 后生成，与 workspace/ 并列
```

## 使用 Codex 运行

从本示例目录启动 Codex（`-m` 换成当前环境可用的模型即可）：

```bash
cd examples/circle-packing-git-apply
codex -m gpt-5.6-terra -c model_reasoning_effort=max
```

然后完整粘贴 [`codex-goal-prompt.md`](codex-goal-prompt.md)。如果不希望设置顶层
截止条件，改用
[`codex-goal-prompt-untimed.md`](codex-goal-prompt-untimed.md)。

## 使用 Pi 运行

同样从本示例目录启动 Pi：

```bash
cd examples/circle-packing-git-apply
pi
```

然后完整粘贴 [`pi-goal-prompt.md`](pi-goal-prompt.md)。如果不希望设置顶层截止
条件，改用 [`pi-goal-prompt-untimed.md`](pi-goal-prompt-untimed.md)。
要在相同的固定候选流程中启用 LLM-Verifier 极简模式，改用
[`pi-goal-prompt-llm-verifier.md`](pi-goal-prompt-llm-verifier.md)。该变体只让 Selection
消费 `quality_score`，不启用 Adaptive allocation。
要启用确定性 Candidate 退休和替换，改用
[`pi-goal-prompt-adaptive-search.md`](pi-goal-prompt-adaptive-search.md)。该变体限制两个
活跃通道和四个累计 Candidate，由 AllocationPolicy 消费 LLM-Verifier 的资源分配评分，
并启用 `search_diagnostic_guidance/v1` Steering。Allocation 始终先执行；只有没有产生
Allocation Plan 时，Steering 才会向当前活动 candidate 投递一次局部纠错、可证伪检验、
严格去重或边界探索建议。它不延迟淘汰或为低价值方向提供保护。示例使用 `mode=apply`，
阈值只用于演示完整配置，尚未针对 Circle Packing 收益标定。

上述四个 prompt 使用 `inner_agent=autoresearch`，每个 candidate 在一次 worker
dispatch 中自主执行多轮修改和 verifier。要验证普通 agent 接口，改用
[`pi-goal-prompt-common.md`](pi-goal-prompt-common.md)：每个 common dispatch 完成一个
有界 attempt、调用一次 `goal_plus_search_run_verifier` 后返回，后续尝试由 main continuation 同一
candidate。common 是空白策略基线：runtime 仍为 MCP verifier 创建 outer attempt commit，
但 candidate workspace 不生成 `results.tsv` 或额外 ledger commit；历史保存在 canonical
`.gp` ledger。

这些 prompt 的正文明确要求进入 Search Mode，并描述问题、允许修改的文件和验收方法。
第一行的 typed config 负责选择运行行为：`workspace_provider=git_worktree` 创建候选 worktree，
`max_parallel=2` 和 `strategy=agent_guided` 控制 Search，`max_duration_seconds=300`
从 Goal 创建时建立持久的五分钟 deadline；候选返回后由 Main 判断是否继续，
`inner_agent` 选择 worker 行为 adapter，`promotion_mode=apply` 将选中的发布提交应用回
源码仓库。prompt 不需要逐步指挥
candidate 创建、selection、promotion、apply 或 report。

prompt 使用当前 Host 已选择的模型和已安装的 Goal Plus 默认值；所有 subagent
（candidate worker、Evidence annotator）默认不指定模型，直接继承 main agent 当前
使用的模型。本示例只关注 Git workspace 和 apply 行为。

## 这个示例展示什么

```text
可写的 workspace/ Git 仓库
              |
              | new
              v
       本次运行的 Git mirror
          /             \
  candidate c001    candidate c002
  worktree/branch   worktree/branch
          \             /
       verifier 验证后的选择
                   |
                   v
          squash 发布提交
                   |
                   | goal_plus_search_apply_promotion
                   v
       fetch + cherry-pick 回 workspace/
```

候选创建使用 Git worktree checkout。最终回写不会复制候选目录，也不会把候选分支
手工 checkout 到源码仓库。Git Provider 只从最佳候选的允许修改文件中构造一个发布
提交，把它 fetch 到 `workspace/` 后再 cherry-pick。完整候选历史保留在 `.gp/` 的
运行仓库中；源码仓库只收到一个可审计的产品提交。

## 检查回写结果

Goal Plus 完成后，直接检查源码仓库：

```bash
python3 workspace/.goal-plus-verifiers/circle_packing_score.py
git -C workspace status --short
git -C workspace log --oneline --decorate -2
git -C workspace show --stat --oneline HEAD
```

回写完成后，源码仓库必须保持干净。如果最佳候选修改了 `initial_program.py`，
`HEAD` 会增加一个发布提交；如果最佳候选与 baseline 完全相同，Provider 会记录
Git no-op，`HEAD` 保持不变。

候选只能修改 `initial_program.py`。公开 verifier 仍然要求在单位正方形内返回恰好
26 个互不重叠的圆；`sum_radii` 达到 `2.2` 即视为流程验证达标（由 SearchSpec 的
`constraints.success_threshold` 声明），达标后正常收尾。任务和评分规则与原始
Circle Packing 示例完全相同。
