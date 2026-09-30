# Candidate Evidence Replay

Evidence replay 是操作者发起的离线实验。它从 Global Evidence 列表已交付、
candidate 尚未作出下一次模型决策的位置分叉。操作者从该列表显式选择非空目标子集；
每个目标启动一个独立分支，并由宿主先投递该目标的真实详情。原轨迹只是观察性参照，
只选择 B 不会自动运行 A，也不会遍历其余 Evidence。

## 可用条件

当前支持 Codex direct candidate 和 `git_worktree`/`copy` workspace。捕获发生在显式
`goal_plus_search_get_global_evidence` 返回或 verifier 的自动列表返回边界。
捕获保存工作区文件、当前 candidate 及所有可引用 Evidence candidate 的独立 Git mirror、
当时的 run/FrozenSpec/Goal 状态、列表及可展开详情。每个 Evidence commit 在捕获完成前
必须能从对应 mirror 解析；已有的开放 reference workspace 会在隔离分支中从 mirror
重新物化，不复用指向原 run 的 Git worktree 控制文件。
原生 turn 完成后，宿主核对原始 rollout 中的 turn、唯一外层 `exec` 返回与列表哈希，
保存到列表结果为止的原始 Responses 项。只允许能够确认是单次 Evidence 工具操作的
外层调用进入 `ready`；无法定位、包含多个操作、前缀缺失或工作区不稳定时返回
`unavailable`，并保留原因。旧 run 没有捕获快照时不能从 trace 补造 ForkPoint。

ForkPoint 保存的是模型可见历史和文件状态，不包含 KV cache、隐藏推理状态或随机种子。
分支之间共享相同的起点和各自相同的剩余相对执行时间；后续采样不保证逐字一致。
详情投递会标记为宿主实验预取，包含真实 candidate/iteration/artifact 和内容哈希，
不会伪造 candidate 自己发起的 `get_evidence_detail` 调用。后续真实详情调用仍由
正常 Search 工具解析身份并记录。实验状态、verifier 和文件只写入分支 runtime。

## 操作命令

```bash
goal-plus replay inspect --root .gp --run-id RUN --candidate-id CANDIDATE
goal-plus replay create --root .gp --fork-point FORK_ID \
  --target CANDIDATE:ITERATION --request-id REQUEST_ID
goal-plus replay status --root .gp --experiment-id EXPERIMENT_ID
goal-plus replay report --root .gp --experiment-id EXPERIMENT_ID
```

`inspect` 按原列表顺序显示 Evidence、可展开目标、ForkPoint 状态和拒绝原因。
`create` 默认立即执行；可加 `--no-run` 只持久化选择，之后用
`goal-plus replay run --root .gp --experiment-id EXPERIMENT_ID` 启动。
重复的 `--target`、列表外目标、身份或详情不一致会拒绝。
同一个 `--request-id` 与同一有序选择幂等；换目标会拒绝。
建议每次创建显式提供 request ID，以便命令重试。

分支状态为 `created`、`running`、`completed` 或 `failed`。
进程异常中断后，重试 `run` 会把遗留的 `running` 分支标记为失败并保留现场，
不会重新发送不确定的模型请求。报告列出每支交付的详情哈希、分叉后 verifier
尝试、首次通过、最佳/最终分数、用量和代码差异路径。单次差异不构成稳定收益证明。

实验结束后可显式清理分支 runtime，再释放 ForkPoint 的 Git pin：

```bash
goal-plus replay cleanup --root .gp --experiment-id EXPERIMENT_ID
goal-plus replay release --root .gp --fork-point FORK_ID
```

`cleanup` 只接受终态分支，保留选择、报告数据和差异文件；不确定中断需要先人工确认
进程已静默，再加 `--confirmed-stopped`。`release` 要求该 ForkPoint 的全部实验已清理，
删除其工作区及 Git
快照并禁止后续创建，保留列表、详情、前缀、元数据和既有报告。

## 边界

- Pi 和 ThinkThread provider 暂不支持精确 replay。
- 原轨迹后续的自主选择与实验预取不是对称干预；只选 B 时不要把原轨迹当作预取 A。
- 模型、依赖或外部服务漂移仍可能影响结果；报告的分数需要结合实际 verifier 记录阅读。
- 真实 Codex host 端到端验证按 [测试约定](../tests/README.md) 单独执行；
  确定性测试不证明模型实际消费了注入详情。
