---
name: auto-harness-adapter
description: 从任务边界与评测器边界发现 agent harness 的可编辑面，并生成供审阅的保守 AHE adapter manifest。当 AHE 任务需要在不动工作负载、kernel、漏洞目标、evaluator 或 golden 数据的前提下优化 harness 时使用。
---

# 自动 Harness Adapter

在启动 AHE 优化前生成 adapter 合同草稿。把 agent harness 当作优化对象；
把任务仓库、工作负载、kernel、evaluator、runner、golden 输出与 benchmark
数据当作只读。

## 七类可编辑组件（AHE 论文）

修改范围遵循 AHE 论文（arXiv:2604.25850 第 3.1 节，NexAU substrate），
其暴露**七类正交的可编辑组件**并落为显式文件。生成的 manifest 必须覆盖
harness 中实际存在的全部七类，而不是只有 prompt 或只有运行时：

| # | 组件 | 典型挂载点 |
|---|---|---|
| 1 | System prompt | `prompts/`、`instructions*`、`assets/**/prompts/` |
| 2 | Tool description | `tools*.md`、`tool_descriptions/`、工具 API 文档 |
| 3 | Tool implementation | `src/`、`lib/`、`runtime/`、工具可执行文件 |
| 4 | Middleware | `middleware/`、`extensions/`（如 `assets/pi/extensions/*.ts`） |
| 5 | Skill | `skills/`、`.agents/skills/`、playbook |
| 6 | Sub-agent 配置 | `agents/`、`assets/**/agents/*.toml` |
| 7 | Long-term memory | `memory/`、ledger/handoff 机制文件 |

论文的消融实验把大部分收益定位在 **tool implementation、middleware 与
long-term memory**（只改 system prompt 反而回退），因此把任何一类锁死的
manifest 都会让 optimizer 吃不到最关键的组件。checkout 中不存在的组件类
直接不出现条目；绝不发明路径。

## 工作流

1. 扫描前先解析两个边界：
   - `harness_root`：包含待优化的 agent prompt、工具、skill、middleware、
     memory、策略或 agent 实现的 checkout。
   - `task_root`：harness 必须解决、绝不编辑的工作负载 checkout（例如
     PyPTO kernel 仓库或漏洞目标）。
2. 提供真实 evaluator 命令。不要把 evaluator 当作 harness 组件，也不要用
   字符串匹配或 API 可用性检查顶替真实分数。
3. 运行 `scripts/discover_adapter.py` 或 AHE surface 模块生成 manifest
   草稿。输出包含高置信度可变组件（覆盖七类 AHE 组件）、供审阅的中等
   置信度文件、排除路径与告警。
4. 对照上面的七组件清单审阅报告。每个面向 harness 的组件类都保留在
   `components` 且 `mutable: true`；移除任何任务、evaluator、runner、
   golden、fixture、模型、kernel 或数据路径。多轮优化前补上真实的
   `validation.command`。
5. 用生成的 manifest 和真实 evaluator 运行 AHE。源 harness 保持不可变；
   候选复制到隔离 workspace，只有通过校验与指标/holdout 检查后才被接受。

## 发现命令

在仓库根运行：

```bash
python plugins/ahe/skills/auto-harness-adapter/scripts/discover_adapter.py \
  /path/to/harness \
  --task-root /path/to/task \
  --task "optimize the harness for the PyPTO task" \
  --eval-cmd "python /path/to/evaluate.py --task-root /path/to/task" \
  --validation-cmd "python -m compileall ." \
  --output-manifest /tmp/ahe-auto-manifest.json
```

脚本刻意保守：只选高置信度的面向 harness 的文件，中等置信度文件只报告、
默认不启用。缺少 validation 或 evaluator 命令是告警，不是发明一个的许可。

## 可变面规则

生成的 manifest 只应包含影响 harness 行为的文件，并按七类 AHE 组件组织、
全部可变：

- system prompt、tool description、工具与运行时实现、middleware/扩展、
  skill、sub-agent 配置、long-term memory（ledger/handoff）与 harness
  配置；
- 明确处于 harness 执行路径上的源码文件。

以下内容保持在可变组件清单之外：

- 任务与工作负载源码，包括 PyPTO kernel；
- 漏洞目标与 fixture；
- evaluator、benchmark、runner、golden 输出与测试数据；
- 生成产物、缓存、依赖、凭证与 lock 文件。

组件模式下，未声明的路径一律只读。除非用户明确批准开放式实验，不要使用
`path: "."` 加 `mutable: true`。

## 适配期间的上网检索

生成 manifest（以及之后的 evolve）期间，鼓励上网检索参考资料：AHE 论文
本身（arXiv:2604.25850）、NexAU substrate、公开 coding-agent harness
（Codex、OpenCode、Terminus）、skill 库与中间件模式。用检索结果判断该
harness 暴露了哪些组件类、如何划定边界；当组件类或挂载点不明显时引用来源。
网络检索只用于指导 manifest——绝不把任务或 evaluator 路径加进可变面。

## AHE 运行

审阅通过后运行：

```bash
python plugins/ahe/ahe.py /path/to/harness \
  --manifest /tmp/ahe-auto-manifest.json \
  --adapter command \
  --task "..." \
  --eval-cmd "..." \
  --holdout-eval-cmd "..."
```

任务根始终在候选写面之外。若发现报告中仍有未决的中等置信度文件，停下来
请求审阅，而不是自动扩大 manifest。

在 Pi 命令中，使用类似请求：

```text
/ahe harness路径为/path/to/harness, 任务根目录为/path/to/task,
自动生成adapter, 输出manifest为/tmp/ahe-auto-manifest.json,
评测器为python /path/to/evaluate.py, 提示词为优化该 harness 完成任务
```
