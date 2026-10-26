# VLIW Kernel Optimization：公开模拟器优化

这个示例使用公开案例和本地 VLIW/SIMD 模拟器，目标是在保持结果正确的前提下降低
生成程序的模拟周期数。它只依赖 Python 标准库，不需要 Docker 或加速器硬件。

本示例采用只读模板模式，不需要运行准备脚本，也不要创建临时 `workspace/`。候选
worktree、Evidence、publication 和最终报告都由 Goal Plus 写入当前目录的 `.gp/`。

> [!IMPORTANT]
> 必须在本示例目录 `vliw-kernel-optimization/` 中启动 Pi，不要进入
> `workspace-template/` 后再启动。受版本控制的 `workspace-template/` 是只读输入，
> Goal Plus 完成后不会把选中结果回写到该目录。

## 目录结构

```text
vliw-kernel-optimization/
|-- README.md
|-- search-spec.json
|-- pi-goal-prompt.md
|-- PROVENANCE.md
|-- LICENSE.edgebench
`-- workspace-template/
    |-- .goal-plus-verifiers/vliw_score.py
    |-- problem.md
    |-- problem.py
    |-- runner.py
    |-- solution.py
    |-- verifier.py
    |-- public_tests/
    `-- test_cases/
```

运行后会新增被 Git 忽略的 `.gp/`，其中保存候选 Evidence、publication、
`report.md` 和 `report.html`。`workspace-template/` 应保持不变。

`git_worktree` 工作区实现会校验包含该模板的外层 Git 仓库是否干净。开始运行前应处理
staged、unstaged 或未忽略的 untracked 文件；`.gp/` 本身已被忽略，不会造成判脏。

## 检查本地 baseline

verifier 会调用 `runner.py`，因此需要以 `workspace-template/` 为工作目录：

```bash
cd examples/vliw-kernel-optimization
(cd workspace-template && python3 .goal-plus-verifiers/vliw_score.py)
```

初始实现应通过正确性检查，并输出 `147734` 个周期。指标 `cycles` 越低越好。
候选只能修改自己的 `solution.py`；模拟器、runner、verifier、案例和 score adapter
都是冻结输入。

也可以在模板目录直接运行公开 runner：

```bash
(cd workspace-template && python3 runner.py)
```

## 使用 Pi 运行

从本示例目录启动 Pi：

```bash
cd examples/vliw-kernel-optimization
pi
```

然后完整粘贴 [`pi-goal-prompt.md`](pi-goal-prompt.md)。

prompt 不固定 `main`、`annotator` 或 `workers`，直接使用 Pi 当前选中的模型和已安装的
Goal Plus 默认值。整个任务最多 8 分钟；候选返回后由 Main 判断是否继续。

## 工作区合同

下文为默认 Pi RPC/Git 入口；TT 的独立配置见本文末尾。

当前目录中的 [`search-spec.json`](search-spec.json) 是权威 SearchSpec。它固定：

- `source_path="workspace-template"`；
- 两个长期候选通道；
- `workspace_provider=git_worktree`；
- `promotion_mode=artifact_only`；
- 只允许修改 `solution.py`；
- 使用 `.goal-plus-verifiers/vliw_score.py` 最小化 `cycles`。

不要创建与 `.gp/` 并列的临时 `workspace/`。Goal Plus 只在 `.gp/` 内创建隔离的候选
worktree；selection 后的 publication 也只作为 artifact 保留，不应用回模板。

## 检查运行结果

完成卡会给出 `run_id`、publication 和报告路径。可以先使用只读 monitor 检查：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"run_id":"run_..."}' \
  --pretty
```

最后确认模板没有变化并复查 baseline：

```bash
git status --short -- workspace-template
(cd workspace-template && python3 .goal-plus-verifiers/vliw_score.py)
```

最终报告应给出选中结果的 verifier-backed `cycles`，以及相对 `147734` 周期 baseline
的改进量。

## 示例边界

这是可运行的本地公开示例，不是 EdgeBench 官方成绩。仓库不包含隐藏 controller
案例，因此它证明的是 Goal Plus 编排、候选优化和公开正确性门禁，而不是隐藏评测上的
可比性。上游来源和许可证见 [`PROVENANCE.md`](PROVENANCE.md)。

## ThinkThread 15 分钟多轮验证

使用 [TT SearchSpec](thinkthread-search-spec.json) 和
[TT Pi prompt](pi-thinkthread-goal-prompt.md)。该变体保留只读模板与 artifact_only，
采用两个 TT + AutoResearch candidate 和 15 分钟 Goal 上限；Main 根据 Evidence 判断续跑。
目标是同一 candidate/Child 多轮结算，同时观察 Annotation、exact artifact、publication 和清理。
它不运行 LLM Verifier/Adaptive Allocation，不能作为这些组件的真实覆盖。

从本示例目录启动受支持的 Profile，state root 必须在 checkout 读取授权范围之外：

```bash
mkdir -p "$PWD/.tmp"
tt_vliw_state=$(mktemp -d "$PWD/.tmp/goal-plus-vliw-tt.XXXXXX")
GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED=1 \
GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED=1 \
tt run --profile goal-plus-pi --fs "$PWD" --state-root "$tt_vliw_state" \
  -- --model zai/glm-5.3-flash --thinking low
```

在 TUI 中完整粘贴 TT prompt，不通过 `-p` 传入。运行记录位于 Pi Session-private root，
不使用本目录 `.gp/`；模板不回写。验收需要核对同一 session/Child 的 dispatch 和 iteration
增长、正确性与 cycles、完整 Annotation、promotion/report 及 Cleanup complete；不足两轮
须明确记录为续跑覆盖缺口。最后精确停止测试 Root，保留所需诊断后再按 TT 生命周期清理。
