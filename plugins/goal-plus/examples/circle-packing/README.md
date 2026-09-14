# Circle Packing：十分钟完整运行

这是推荐的 Goal Plus 快速体验示例。它只依赖 Python 标准库，通过公开 verifier 检查
26 个圆的合法性并计算半径和。Pi 和 Codex 都会启动两个固定候选通道；限时版使用十分钟
任务预算，不限时版用于单独观察完成语义。

本示例采用只读模板模式，不需要运行准备脚本，也不要创建临时 `workspace/`。候选
worktree、Evidence、publication 和最终报告都由 Goal Plus 写入当前目录的 `.gp/`。

> [!IMPORTANT]
> 必须在本示例目录 `circle-packing/` 中启动 Codex 或 Pi，不要进入
> `workspace-template/` 后再启动。受版本控制的 `workspace-template/` 是只读输入，
> Goal Plus 完成后也不会把选中结果回写到该目录。

## 目录结构

运行前：

```text
circle-packing/
|-- search-spec.json
|-- workspace-template/
|   |-- .goal-plus-verifiers/circle_packing_score.py
|   `-- initial_program.py
|-- codex-goal-prompt.md
|-- codex-goal-prompt-untimed.md
|-- pi-goal-prompt.md
`-- pi-goal-prompt-untimed.md
```

运行后会新增被 Git 忽略的 `.gp/`：

```text
circle-packing/
|-- workspace-template/   # 保持不变
`-- .gp/
    `-- runs/<run_id>/   # 候选 Evidence、publication、report.md、report.html
```

`git_worktree` 工作区实现会校验包含该模板的外层 Git 仓库是否干净。开始运行前应处理
staged、unstaged 或未忽略的 untracked 文件；`.gp/` 本身已被忽略，不会造成判脏。

## 检查本地 baseline

在示例目录运行：

```bash
cd examples/circle-packing
python3 workspace-template/.goal-plus-verifiers/circle_packing_score.py
```

初始程序应通过合法性检查，并输出约 `1.79835` 的 `sum_radii`。指标越高越好。
候选只能修改自己的 `initial_program.py`；verifier 和其他模板文件都是冻结输入。

## 使用 Codex 运行

从本示例目录启动 Codex：

```bash
cd examples/circle-packing
codex -m gpt-5.6-terra -c model_reasoning_effort=max
```

然后完整粘贴 [`codex-goal-prompt.md`](codex-goal-prompt.md)。该版本只设置十分钟
整个任务预算，不在用户 prompt 中规定候选停止时间或单个 worker timeout。

如果不希望设置顶层截止条件，改用
[`codex-goal-prompt-untimed.md`](codex-goal-prompt-untimed.md)。

Circle 的 Codex prompt 是示例集中唯一的显式多模型路由：一个候选通道使用 Terra，
另一个使用 Luna，并由 Terra 执行 Evidence annotation。

## 使用 Pi 运行

同样从本示例目录启动 Pi：

```bash
cd examples/circle-packing
pi
```

然后完整粘贴 [`pi-goal-prompt.md`](pi-goal-prompt.md)。不限时版本使用
[`pi-goal-prompt-untimed.md`](pi-goal-prompt-untimed.md)。

Pi prompt 不固定 `main`、`annotator` 或 `workers`，直接使用 Pi 当前选中的模型和
已安装的 Goal Plus 默认值。

## 工作区合同

当前目录中的 [`search-spec.json`](search-spec.json) 是权威 SearchSpec。它使用
`source_path="workspace-template"`、`workspace_provider=git_worktree` 和
`promotion_mode=artifact_only`：

- `workspace-template/` 始终只读。
- 不创建与 `.gp/` 并列的临时 `workspace/`。
- Goal Plus 只在 `.gp/` 内创建候选 worktree。
- selection 后生成 publication artifact，但不应用回模板。
- 任务终态后生成一次 `report.md` 和 `report.html`。

十分钟配置是整个 Goal Plus 任务的 deadline。候选返回后由 Main 根据 Evidence 判断
是否继续；没有固定 worker 时长或提前收尾窗口。时间预算不改变结果有效性的验收标准。

## 检查运行结果

完成卡会给出 `run_id`、publication 和报告路径。可以先使用只读 monitor 检查：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

最后确认模板没有变化：

```bash
git status --short -- workspace-template
python3 workspace-template/.goal-plus-verifiers/circle_packing_score.py
```

这个示例使用公开反馈演示优化流程，不是隐藏答案 benchmark。
