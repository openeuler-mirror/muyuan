# Ultra 编排

Goal Plus 在 Codex 中通过 `$goal-plus`、在 Pi 中通过 `/goal-plus` 使用同一套 host-neutral 执行合同：

```text
host-selected main reasoning + proactive delegation + automatic Search routing + until terminal
```

runtime 负责 work DAG、有 Evidence 支持的状态转换、Search 关联和 completion
gate；host 负责启动、等待、通信、中断和原生日志。core 不直接调用 Codex、Pi
或模型 provider。

## Work DAG

triage 完成后，main 调用 `goal_plus_upsert_work_items` 建立当前 revision 的 work DAG。每个 work item 选择一种 route：

| Route | Owner 与用途 |
| --- | --- |
| `main` | 共享状态、架构、集成和最终验证 |
| `subagent` | 由当前 host 派发的有界独立工作 |
| `search` | 通过现有 Search Mode 做 verifier-guided exploration |

依赖项必须先进入 `accepted`，下游 work item 才能派发。所有 required work item
都被接受后，才能进入 final review 和 `complete`。main 始终负责检查结果、决定
`accepted` 或 `rework`，以及完成最终集成。

只有同时具备数值 metric、确定性 verifier、隔离 edit surface、多个值得尝试的
hypothesis 和足够 budget 时，才把 work item route 到 Search。创建并关联 run 后记录
`search_routed`；Search 选出的结果仍需 main 接受。

## Subagent Attempt

`route="subagent"` 的状态流为：

```text
planned -> launching(attempt, generation, TTL) -> active(bound handle)
        -> result_ready -> accepted
```

`dispatch` 生成 `attempt_id`、`generation` 和 launch deadline，但不代表 worker
已启动。host 原生启动成功后再记录 `bind`，并在后续 `message`、`result` 或
`failed` event 中携带同一 attempt identity。

已绑定且处于 `result_ready` 的 attempt 可以通过 `rework` 继续；尚未绑定的
blocked/failed work 会回到 `planned`，由新 attempt 接手。若 `launching` 超时，
host 可以重新派发并递增 generation，以恢复启动阶段崩溃。旧 generation 迟到的
终态输出会保留为 `stale_result` Evidence，但不能修改当前 work item；当前
attempt 的相同结果可幂等重放，内容冲突的重复结果会被拒绝。

## Host 合同

Ultra host binding 使用 `goal-plus-ultra-v1` protocol。`spawn`、`wait` 和
`observe` 是必需能力，`send` 和 `interrupt` 是可选的原生能力。
`main_reasoning_effort` 记录 host 是要求 `max`，还是保留用户的
`host_selected` 档位；每种 host 还要记录实际传给原生系统的值，以及由谁负责
约束。

### Codex

Codex 绑定 `native_reasoning_effort="max"`。project hook 无法修改已经开始的
第一个 turn，因此 host 必须在 main 启动前选好 `max`。普通 subagent 通过 Codex
collaboration tool 启动和管理。

### Pi

Pi 保留提交 `/goal-plus` 时 Main 已选择的 thinking level，并记录
`main_reasoning_effort="host_selected"` 和实际档位；binding 不强制提升或拒绝某个档位。
复杂任务推荐 `xhigh`。`pi_goal_plus_run_work_item` 让隔离的 Pi RPC child 继承
Main 当前档位并把结果返回 main，多次独立 tool call 可以并发形成多条 lane。

### 其他 Harness

DeepSeek 或其他 harness 在创建 Goal Plus record 时提供同一份 profile，例如：

```json
{
  "execution": {
    "host": {
      "protocol": "goal-plus-ultra-v1",
      "host_id": "deepseek-harness",
      "native_reasoning_effort": "highest",
      "enforcement": "harness",
      "operations": ["spawn", "wait", "observe"]
    }
  }
}
```

harness 必须在第一个 main turn 前设置其最高 reasoning budget，通过自身 worker
API 派发 work item，并用 `goal_plus_record_work_event` 记录状态转换。provider
名称和 command schema 不进入 core state machine。

## 监控

main/worker 交互监控是可选的只读投影，不是 Ultra lifecycle stage。host 可以在
work event 的 `orchestration_monitor` namespace 中记录原生操作；
`orchestration` feature plugin 会把不同 host 的 task、result 和 decision 投影为
统一结构，但不会接管 worker process。

监控字段和开关见 [Feature Plugin](feature-plugins.md)，host-specific 启动方式见
[Codex](codex.md) 和 [Pi](pi.md)，完整工具合同见 [API](api.md)。
