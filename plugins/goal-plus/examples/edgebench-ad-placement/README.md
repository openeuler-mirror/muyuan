# EdgeBench Ad Placement：公开 C++ 布局优化

这个示例使用公开、确定性的合成案例，复现 EdgeBench
`ad_placement_optimization` 的提交和命令行合同。目标是在保持十个公开案例输出合法的
前提下，提高广告矩形布局总分。

本示例采用只读模板模式，不需要运行准备脚本，也不要创建临时 `workspace/`。候选
worktree、Evidence、publication 和最终报告都由 Goal Plus 写入当前目录的 `.gp/`。

> [!IMPORTANT]
> 必须在本示例目录 `edgebench-ad-placement/` 中启动 Pi，不要进入
> `workspace-template/` 后再启动。受版本控制的 `workspace-template/` 是只读输入，
> Goal Plus 完成后不会把选中结果回写到该目录。

## 目录结构

```text
edgebench-ad-placement/
|-- README.md
|-- search-spec.json
|-- pi-goal-prompt.md
|-- historical-python-fixture-report.md
`-- workspace-template/
    |-- .goal-plus-verifiers/ad_local_score.py
    |-- solution.cpp
    `-- tools/
        `-- bin/
            |-- gen
            `-- tester
```

运行后会新增被 Git 忽略的 `.gp/`，其中保存候选 Evidence、publication、
`report.md` 和 `report.html`。`workspace-template/` 应保持不变。

`git_worktree` 工作区实现会校验包含该模板的外层 Git 仓库是否干净。开始运行前应处理
staged、unstaged 或未忽略的 untracked 文件；`.gp/` 本身已被忽略，不会造成判脏。

## 任务合同

候选只提交和修改 `solution.cpp`，并使用下面的公开合同：

- 使用 `g++ -std=c++17 -O2` 编译；
- 输入第一行是 `n`，随后是 `n` 行 `x y r`；
- 输出是 `n` 行整数矩形 `x1 y1 x2 y2`；
- 每个矩形必须包含对应的 `(x_i + 0.5, y_i + 0.5)`；
- 所有矩形必须位于 `10000 x 10000` 网格内且互不重叠；
- 每个本地案例的 solver 限制为 5 秒；
- 工作环境是 CPU-only、离线、1 GB 内存。

面积为 `s_i`、目标面积为 `r_i` 时，单个合法矩形的满意度为：

```text
1 - (1 - min(r_i, s_i) / max(r_i, s_i))^2
```

tester 输出最接近
`1,000,000,000 * sum(satisfaction) / n` 的整数。

本地 generator 接收 seed 文件：

```bash
cd examples/edgebench-ad-placement/workspace-template
printf '0\n' > /tmp/ad-seeds.txt
rm -rf /tmp/ad-cases
./tools/bin/gen /tmp/ad-seeds.txt -d /tmp/ad-cases
```

编译并检查单个案例：

```bash
g++ -std=c++17 -O2 solution.cpp -o /tmp/ad-solution
/tmp/ad-solution < /tmp/ad-cases/0000.txt > /tmp/ad-output.txt
./tools/bin/tester /tmp/ad-cases/0000.txt /tmp/ad-output.txt
# stderr: Score = <number>
```

`tools/bin/gen`、`tools/bin/tester` 和 `.goal-plus-verifiers/` 都是只读评测资产。

## 检查本地 baseline

从示例根目录运行公开 verifier：

```bash
cd examples/edgebench-ad-placement
(cd workspace-template && python3 .goal-plus-verifiers/ad_local_score.py)
```

verifier 会编译 `solution.cpp`，评估十个公开案例，并输出
`{"local_score_sum": ...}`。指标越高越好。

## 使用 Pi 运行

从本示例目录启动 Pi：

```bash
cd examples/edgebench-ad-placement
pi
```

然后完整粘贴 [`pi-goal-prompt.md`](pi-goal-prompt.md)。

prompt 不固定 `main`、`annotator` 或 `workers`，直接使用 Pi 当前选中的模型和已安装的
Goal Plus 默认值。整个任务最多 8 分钟；候选返回后由 Main 判断是否继续。

## 工作区合同

当前目录中的 [`search-spec.json`](search-spec.json) 是权威 SearchSpec。它固定：

- `source_path="workspace-template"`；
- 两个长期候选通道；
- `workspace_provider=git_worktree`；
- `promotion_mode=artifact_only`；
- 只允许修改 `solution.cpp`；
- 使用 `.goal-plus-verifiers/ad_local_score.py` 最大化 `local_score_sum`。

冻结 spec 时，至少把 score adapter、`tools/bin/gen` 和 `tools/bin/tester` 作为 verifier
资产。不要调用 EdgeBench 隐藏 judge。

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
(cd workspace-template && python3 .goal-plus-verifiers/ad_local_score.py)
```

## 示例边界

本地 tester 实现公开的 AHC 风格合法性和满意度公式，但案例是合成公开数据，不是
EdgeBench 官方隐藏 judge。分数只能证明本地流程有效，不能与官方排行榜直接比较。

[`historical-python-fixture-report.md`](historical-python-fixture-report.md) 保存的是旧版
Python 形态 fixture 的历史证据，其中的分数和文件名不适用于当前 C++ 示例。当前运行的
报告只生成在被忽略的 `.gp/` 目录中。
