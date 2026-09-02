# Monitor Feature Plugin 使用说明

Goal Plus 的 monitor feature plugin 基于持久化 goal record 和只追加 event log 生成只读投影。它可以向 `goal_plus_monitor_snapshot.feature_plugins` 增加派生视图，但不会启动、等待、通信、中断或监督 worker。

plugin 默认启用。调用方也可以明确选择，或关闭全部 feature plugin：

```text
goal_plus_monitor_snapshot(feature_plugins=["orchestration"])
goal_plus_monitor_snapshot(feature_plugins=[])
```

未知名称会直接报错，避免监控范围被静默改变。

## Orchestration View 内容

内置 `orchestration` plugin 把实际发生的普通 subagent 派发投影为一套 host-neutral 交互结构：

```text
assignment -> worker_update(bound) -> result -> decision
```

每条 dispatch 包含 assignment summary、不透明的 worker 标识、当前状态和带方向的 event 摘要。attempt 部分还包含 generation、`launching`/`bound` 状态、launch deadline 和 stale-result 数量。旧 generation 仍以 `stale_result` 显示，但不会替换当前 worker。

投影不包含 transcript 内容或路径，只返回 `transcript_available` 布尔值。它展示的是已持久化的协调事实，不推断模型意图，也不参与 work item 状态转换。

## Host Metadata 约定

原生通信仍由 host 所有。host 通过既有的 `goal_plus_record_work_event.metadata` 记录实际 operation 名称：

```json
{
  "orchestration_monitor": {
    "native_operation": "spawn_agent",
    "direction": "main_to_subagent",
    "semantic_event": "assignment"
  }
}
```

只会暴露符合 identifier 形式的 operation 名；类似 prompt 或格式异常的值会被忽略。`direction` 和 `semantic_event` 是受限的可选 override，省略时由持久 work-event kind 推导。

Codex 通常记录 `spawn_agent`、`wait_agent` 和 `followup_task`；Pi wrapper 记录 `pi_goal_plus_run_work_item` 和 `goal-plus-pi-worker`。两者的原生命令和等待方式不同，但 assignment、result 和 main-owned decision 语义可以直接比较。

仓库提供一条确定性的协议对比：

```bash
python scripts/compare_orchestration_hosts.py \
  .tmp/orchestration-monitor-comparison
```

该诊断脚本验证持久化协议，不是 live-model quality benchmark。
