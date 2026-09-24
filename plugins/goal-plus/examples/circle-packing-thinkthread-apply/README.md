# Circle Packing: ThinkThread Shared-Plane Apply

这个示例在 [0/1 ThinkThread apply smoke](../guess-number-thinkthread-apply/README.md) 之后运行。
它用两个公开、确定的布局路线启动 TT outer candidate，验证 snapshot settlement、评分、
选择和最终 Root 回写能在同一 Goal Plus run 中完成。Pi 路径使用 AutoResearch-Git inner
candidate，Codex 路径使用 Common inner candidate。

两个实现作为只读提示公开，避免模型偶然性掩盖编排问题：

- c001 使用 5 x 6 矩形格，预期 `sum_radii` 约为 2.166664；
- c002 使用 5/4 交错六角格，预期 `sum_radii` 约为 2.438964。

```text
Pi /goal-plus 或 Codex goal_plus_host_start in Root
  -> one K=2 ThinkThread plan
     -> c001 rectangular.py -> verify -> Pi 可继续 / Codex 返回
     -> c002 staggered.py   -> verify -> Pi 可继续 / Codex 返回
  -> select best verified iteration -> apply to Root -> Root verifier
  -> terminal reports and host cleanup
```

## 前置条件

需要 Linux、支持所需接口的 ThinkThread Agent Runtime，以及 Pi 或 Codex。按要运行的宿主
生成对应 Profile：

```bash
cd /path/to/muyuan/plugins/goal-plus
./install.sh --pi --thinkthread
tt profile show goal-plus-pi

./install.sh --codex --thinkthread
tt profile show goal-plus-codex
```

版本、protocol 声明或 fingerprint 偏离已测基线时只警告；Profile 结构、实际接口与
delegated capability 必须通过检查。本例不允许
回退到其他执行托管方式或工作区实现；agent 身份由所选 Profile 固定。

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

随后在 Root TUI 完整粘贴 [`pi-goal-prompt.md`](pi-goal-prompt.md)。要与 git-apply 示例对齐（不限时、无候选提示、允许自主探索）时，改用
[`pi-goal-prompt-untimed.md`](pi-goal-prompt-untimed.md)。不要使用
`tt run -- -p ...` 传入 Root prompt；追加的 agent 参数会被 `profile="self"` candidate Child
继承。探索预算为 20 分钟；Main 根据返回的 Evidence 决定是否继续。预算耗尽后不再
启动新探索，只允许已有合同规定的收尾；已提交结果的结算、publication、审计和资源清理
可能需要额外时间，详见[预算与收尾](../../docs/runtime-logic.md#预算与收尾)。

首轮只复制各自的 hint 并立即提交正式 verifier；数值优化留到同一个 Child 的后续 continuation。
完整采用示例 SearchSpec，保留 Annotation 配置。

使用 GLM Flash 时，在上述 `tt run` 命令末尾追加
`-- --model zai/glm-5.3-flash --thinking low`，让 Pi 选择模型；TT 自身的 `--model` 使用另一份
模型注册表。显式指定 `--state-root` 时，目录必须在 Profile 的 checkout 读取授权范围之外，
例如通过 `mktemp -d /tmp/goal-plus-circle-tt.XXXXXX` 创建，再将路径传给 `tt run`。

基础 session 诊断默认采集；需要额外统计请求和返回大小时，在启动命令前增加
`GOAL_PLUS_DEBUG=1`。结束后使用
[session 分析](../../docs/debugging-runtime.md#分析工具描述与调用失败) 对照实际请求中的
MCP 描述、参数 schema、工具错误与修正。此开关仅用于诊断，不是示例启动前置条件。

## 运行真实 Codex E2E

先按上面的步骤准备一次性 Root。需要覆盖 Codex 主模型时，在安装 Profile 时投影对应的
Codex 配置层，例如：

```bash
cd /path/to/muyuan/plugins/goal-plus
./install.sh --codex --thinkthread --codex-profile luna

cd examples/circle-packing-thinkthread-apply
GOAL_PLUS_SUPPLEMENTAL_EVALUATION_ENABLED=1 \
GOAL_PLUS_SUPPLEMENTAL_EVALUATION_REQUIRED=1 \
tt run --profile goal-plus-codex --fs "$PWD/workspace"
```

随后在 Root TUI 完整粘贴 [`codex-goal-prompt.md`](codex-goal-prompt.md)。ThinkThread Codex
Root 不加载普通 Codex plugin command 和 hooks，因此这里不使用 `$goal-plus`；prompt 会要求
Root 先通过受信 MCP 的 `goal_plus_host_start` 建立 Goal，再按共同状态机推进。

Codex binding 当前使用 `inner_agent=common`。Codex prompt 只覆盖示例 SearchSpec 的这一字段，
保留 ThinkThread workspace、K=2、Annotation 和 apply 合同；两个候选各提交一次确定性 hint
即可进入选择和回写，不要求额外 improvement turn。不要通过 `tt run -- -p ...` 或追加 Codex
参数切换模型，因为 Root 参数会被 candidate 继承；使用安装器的 `--codex-profile` 投影模型。

## 验收

完成卡出现后检查 Root：

```bash
(cd workspace && python3 .goal-plus-verifiers/circle_packing_score.py)
```

用户侧验收：

- Goal Plus 为 `complete`，恰好一个 run、一个 K=2 plan 和两个 candidate；
- Pi FrozenSpec 明确记录 `inner_agent=autoresearch`；Codex FrozenSpec 明确记录
  `inner_agent=common`；
- c001/c002 均得到有效 verifier 分数；Pi 可在全局停止条件允许时 continuation 同一个
  candidate，Codex 示例各交付一次后直接收尾；
- canonical history 位于 native Session-private Goal Plus root，Evidence 是 exact TT snapshot；
  Pi AutoResearch candidate 另有 independent private `.git` 和物理 `results.tsv`，但二者不成为
  公共 Evidence identity；Codex Common candidate 不创建这两项 inner state；
- 选择实际最高有效分数的 iteration，乐观 `fs.apply` 为 `applied`，Root 复验 valid 且得分至少 2.2；
- `report.md`、`report.html` 存在，完成卡显示 `Cleanup complete`。

Child 隔离、完整 Annotation Artifact View、peer comparison、provider resource ownership 和
snapshot 删除属于运行时系统合同，已经由
[`tests/st_thinkthread/test_linux_v3.py`](../../tests/st_thinkthread/test_linux_v3.py) 的真实
ThinkThread 系统验收覆盖；普通示例运行不读取 session-private 内部状态来重复验证这些细节。

运行状态位于 native Session-private Goal Plus root，不写入 `workspace/.gp`。检查完成后停止 Root，从
`tt ps --forest` 取得准确 ID，再执行 `tt rm <root-id>`。不要提交 Runtime state、raw transcript、
socket、token 或生成的 workspace。

## 调试转录在哪里、怎么导出到本目录

TT 只把 `workspace/` 挂载为 Root 的可写文件系统，Goal Plus runtime 因此不在示例目录的
`.gp/`（与 [git-apply 示例](../circle-packing-git-apply/README.md) 不同），而位于 Root 的
受管 session 目录：

```text
~/.local/state/thinkthread/instances/<root-id>/thinkthreads/<root-id>/session/goal-plus/
```

`<root-id>` 是本次 `tt run` 的实例 id（`tt ps --forest` 可查）。需要人工 review 行为时，
把完整转录导出成和 git-apply 示例相同的目录布局——本目录 `.gp/host-logs/agent-transcript/`
（只读调试快照，不是 runtime root，不要用它做恢复）：

```bash
cd ../../../  # plugins/goal-plus
ROOT=$(ls -td ~/.local/state/thinkthread/instances/*/thinkthreads/*/session/goal-plus | head -1)
PYTHONPATH=src python scripts/export_agent_transcript.py "$ROOT" \
  --output examples/circle-packing-thinkthread-apply/.gp/host-logs/agent-transcript
```

TT Child 的 thinking 与原生工具正文只在保留期存在于
`<instance>/thinkthreads/<child>/session/active.jsonl`（controller 沙箱内不可见，释放即
删）。要实时保全全内容，在宿主侧伴随运行 `--watch` 导出（另开一个终端）：

```bash
# 起跑时用包装器（GOAL_PLUS_DEBUG=1 自动伴随 watch）：
cd ../.. && GOAL_PLUS_DEBUG=1 scripts/run-with-transcript.sh tt run --profile goal-plus-pi \
    --fs examples/circle-packing-thinkthread-apply/workspace -- --model zai/glm-5.3-flash --thinking low
# 或对已启动的运行手动挂 watch：
ROOT=$(ls -td ~/.local/state/thinkthread/instances/*/thinkthreads/*/session/goal-plus | head -1)
PYTHONPATH=src python scripts/export_agent_transcript.py "$ROOT" --watch
```

清理实例前先完成导出：`tt rm <root-id>` 会连同 `events.sqlite3` 和 Root 会话一起删除，
那是多数调试数据的唯一副本。
不要手工移动或修改受管目录里的 runtime 状态；上面的导出命令只读。本示例 prompt 文件
不写调试路径，避免进入模型上下文。
