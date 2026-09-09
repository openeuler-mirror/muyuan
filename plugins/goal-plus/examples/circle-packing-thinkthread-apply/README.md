# Circle Packing: ThinkThread Shared-Plane Apply

这个示例在 [0/1 ThinkThread apply smoke](../guess-number-thinkthread-apply/README.md) 之后运行。
它用两个公开、确定的布局路线启动 TT outer + AutoResearch-Git inner candidate，验证
Child-private Git、物理 `results.tsv`、多轮 outer snapshot settlement、评分、选择和最终 Root
回写能在同一 Goal Plus run 中完成。

两个实现作为只读提示公开，避免模型偶然性掩盖编排问题：

- c001 使用 5 x 6 矩形格，预期 `sum_radii` 约为 2.166664；
- c002 使用 5/4 交错六角格，预期 `sum_radii` 约为 2.43893。

```text
Pi /goal-plus in Root
  -> one K=2 ThinkThread plan
     -> c001 rectangular.py -> verify -> feedback -> continue same Child
     -> c002 staggered.py   -> verify -> feedback -> continue same Child
  -> select c002 -> apply to Root -> Root verifier
  -> terminal reports and host cleanup
```

## 前置条件

需要 Linux、Pi、匹配支持基线的 ThinkThread Runtime，以及当前 checkout 生成的
`goal-plus-pi` Profile：

```bash
cd /path/to/muyuan/plugins/goal-plus
./install.sh --pi --thinkthread
tt profile show goal-plus-pi
```

Profile、protocol、fingerprint 或 delegated capability 任一不匹配时必须 fail closed。本例不允许
fallback 到其他 worker host 或 workspace backend。

## 准备一次性 Root

```bash
cd examples/circle-packing-thinkthread-apply
python3 prepare_workspace.py
(cd workspace && python3 .goal-plus-verifiers/circle_packing_score.py)
```

初始 `sum_radii` 约为 1.79835。脚本把共享 Circle Packing 模板、两个 candidate hint 和本例
SearchSpec 复制到被忽略的 `workspace/`。已有目标不会被覆盖；只有本脚本 marker 存在时才允许：

```bash
python3 prepare_workspace.py --reset
```

`--reset` 会删除整个已识别的 workspace 后重新复制，不保留上轮文件。请先停止使用该
workspace 的 Root，再执行重建。公共 verifier 的本地基线执行不会写入 Python 字节码缓存；
旧版本生成的 `__pycache__` 会随重建清除。

## 运行真实 Pi E2E

从本示例目录启动 Root，并显式冻结 supplemental evaluation policy：

```bash
GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED=1 \
GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED=1 \
tt run --profile goal-plus-pi --fs "$PWD/workspace"
```

随后在 Root TUI 完整粘贴 [`pi-goal-prompt.md`](pi-goal-prompt.md)。不要使用
`tt run -- -p ...` 传入 Root prompt；追加的 agent 参数会被 `profile="self"` candidate Child
继承。Goal 最长 20 分钟，candidate lease 最长 3 分钟，并预留 5 分钟完成 annotation、selection、
apply、审计和报告。

首轮只复制各自的 hint 并立即提交正式 verifier；数值优化留到同一个 Child 的后续 continuation。
完整采用示例 SearchSpec，保留 Annotation 配置与收尾预留时间。

使用 GLM Flash 时，在上述 `tt run` 命令末尾追加
`-- --model zai/glm-5.3-flash --thinking low`，让 Pi 选择模型；TT 自身的 `--model` 使用另一份
模型注册表。显式指定 `--state-root` 时，目录必须在 Profile 的 checkout 读取授权范围之外，
例如通过 `mktemp -d /tmp/goal-plus-circle-tt.XXXXXX` 创建，再将路径传给 `tt run`。

## 验收

完成卡出现后检查 Root：

```bash
(cd workspace && python3 .goal-plus-verifiers/circle_packing_score.py)
```

用户侧验收：

- Goal Plus 为 `complete`，恰好一个 run、一个 K=2 plan 和两个 candidate；
- FrozenSpec 明确记录 `inner_agent=autoresearch`，TT binding 使用 Pi Search worker prompt；
- c001/c002 均得到有效 verifier 分数，并在全局停止条件允许时 continuation 同一个 candidate；
- canonical history 位于 `.gp`，Evidence 是 exact TT snapshot；每个 AutoResearch Child 另有
  independent private `.git` 和物理 `results.tsv`，但二者不成为公共 Evidence identity；
- c002 被选择，乐观 `fs.apply` 为 `applied`，Root 复验 valid 且得分至少 2.4；
- `report.md`、`report.html` 存在，完成卡显示 `Cleanup complete`。

Child 隔离、完整 Annotation Artifact View、peer comparison、provider resource ownership 和
snapshot 删除属于运行时系统合同，已经由
[`tests/st_thinkthread/test_linux_v3.py`](../../tests/st_thinkthread/test_linux_v3.py) 的真实
ThinkThread 系统验收覆盖；普通示例运行不读取 session-private 内部状态来重复验证这些细节。

运行状态位于 Pi Session-private Goal Plus root，不写入 `workspace/.gp`。检查完成后停止 Root，从
`tt ps --forest` 取得准确 ID，再执行 `tt rm <root-id>`。不要提交 Runtime state、raw transcript、
socket、token 或生成的 workspace。
