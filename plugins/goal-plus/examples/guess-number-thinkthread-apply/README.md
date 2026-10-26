# Guess Number: ThinkThread Apply E2E

这个示例只回答一个问题：真实 Pi `/goal-plus` 能否完整穿过 ThinkThread Search、选择和
Root 回写，而不是 candidate 是否足够聪明。目标数、评分和两个候选实现全部公开：c001 写入
`print(0)` 得 0 分，c002 写入 `print(1)` 立刻得 1 分并命中目标。

```text
Pi /goal-plus in Root
  -> Goal Plus triage + frozen SearchSpec
  -> one K=2 plan
     -> c001 -> verified score 0
     -> c002 -> verified score 1
  -> select c002 -> promote -> apply
  -> Root guess.py becomes print(1)
  -> terminal Goal Plus -> reports and host cleanup
```

`verify.py` 故意只有一行代码。这个例子适合检查 Host、capability、snapshot、publication 和
cleanup 合同，不适合衡量模型优化能力，也不把补充 Annotation Artifact View 作为通过条件。
selection 会按正常合同关闭任何尚未完成的 best-effort annotation queue；完整共享平面和
Annotation Artifact View 应由后续 Circle 类场景单独验收。

## 前置条件

需要 Linux、Pi、接口兼容的 ThinkThread Runtime，以及由本 checkout 生成的
`goal-plus-pi` Profile：

```bash
cd /path/to/muyuan/plugins/goal-plus
./install.sh --pi --thinkthread
tt profile show goal-plus-pi
```

release、protocol 声明或 fingerprint 偏离已测基线时只警告；Profile 结构、实际接口或
delegated capability 不满足合同则拒绝执行。本例不允许回退到其他工作区实现。

## 准备一次性 Root

在示例目录运行：

```bash
cd examples/guess-number-thinkthread-apply
python3 prepare_workspace.py
(cd workspace && python3 verify.py)
```

初始输出是 `{"guess_score": -1}`。准备脚本生成：

```text
workspace/
|-- .guess-number-thinkthread-example
|-- guess.py
|-- search-spec.json
`-- verify.py
```

如果目标已存在，脚本拒绝覆盖。只有目标包含本例 marker 时，下面的命令才会删除并重建它：

```bash
python3 prepare_workspace.py --reset
```

## 运行真实 Pi E2E

交互运行时，从本示例目录启动 Root：

```bash
tt run --profile goal-plus-pi --fs "$PWD/workspace"
```

然后完整粘贴 [`pi-goal-prompt.md`](pi-goal-prompt.md)。不要把 Root prompt 作为
`tt run -- -p ...` 的追加参数；这些参数属于每次 agent invocation，
会被 `profile="self"` 的 candidate Child 继承。真实 K=2 运行应在 Root TUI 中提交一次
`/goal-plus` 命令，让每个 Child 只接收 Host 生成的 candidate initial message。

prompt 使用 `mode=probe`，因为两个预置候选各自提交一次有效 Evidence 后，编排目标已经完成，
不应继续花时间探索。整个 Goal 最长 10 分钟，Main 在 deadline 内完成 selection、apply、
终态审计和 report；候选无需等待固定时长，这个上限不改变 verifier 标准。

## 验收

Pi 完成卡出现后，在 Root 工作区运行：

```bash
(cd workspace && python3 verify.py)
cat workspace/guess.py
```

产品侧必须看到 1 分和 `print(1)`。持久 Goal Plus Evidence 还必须证明：

- Goal Plus record 为 `complete`，且恰好链接一个 promoted Search run；
- 只有一份 K=2 plan，c001/c002 的 verifier 分数分别为 0 和 1，选择 c002；
- publication 状态为 `applied`，Root 基线未并发变化时由乐观 `fs.apply` 成功回写；
- 最终 `report.md` 和 `report.html` 均存在，完成卡显示 `Cleanup complete`。

Child/private branch 隔离、exact snapshot Evidence 和 provider cleanup 属于运行时系统合同，已经由
[`tests/st_thinkthread/test_linux_v3.py`](../../tests/st_thinkthread/test_linux_v3.py) 的真实
ThinkThread 系统验收覆盖；普通示例运行不读取 session-private 内部状态来重复验证这些细节。

受管 Profile 把 `.gp` 放在 Pi Session-private 目录，不污染 `workspace/`。ThinkThread Root
停止后仍是 Host 管理的持久资源；检查完成后从 `tt ps --forest` 取得准确 Root ID，再执行
`tt rm <root-id>`。不要提交 Runtime state、session、`.gp/`、socket、token 或 transcript。
