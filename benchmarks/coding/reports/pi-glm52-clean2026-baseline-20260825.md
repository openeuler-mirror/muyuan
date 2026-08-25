# pi + GLM-5.2 在 `_clean2026` 上的基线

**这是 `pi` 适配器的第一次成规模测量。基线是 `prod-pi-43f69d86`：45 条，通过 16 条，35.6%。**

同一配置跑了**两次**。第二次（基线）35.6%，第一次 31.1%，**轮间极差 4.4pp**，
两轮不一致的只有 2 条。两轮都不足以给任何模型或 agent 排序 —— 理由见 §7.2。

每一个数都能从随仓库分发的
[`clean2026-pi-glm52-2runs_20260825.json`](../benchmarks/ai_coding/calibrations/clean2026-pi-glm52-2runs_20260825.json)
算出来，复算配方见 §8.2。原始 `results.jsonl` 未发布 —— 它的 `grade.detail` 带 pytest 回溯。

---

## 1. 实验概览

| 项目 | 内容 |
|---|---|
| 实验名称 | `prod-pi` |
| Benchmark | `AI-Coding-Assist` |
| Case 集合 | `_clean2026`（随仓库分发），45 条，全部 `task_type: bugfix` |
| 集合指纹 | `9149d02169845dc5`，run 配置声明值与实测值一致 |
| 被测对象 | `pi` adapter v1.0.0 驱动 pi CLI **0.84.3** |
| 模型 | GLM-5.2（OpenAI 兼容网关） |
| **基线运行** | `prod-pi-43f69d86`，代码 **`ee7ff7e`**，harness `69d797ee`，2026-08-25 18:45–19:11（25.1 min） |
| 第二次观测 | `prod-pi-558ef312`，代码 `c839524`，harness `e830e2ea`，同日 17:48–18:14（25.4 min） |
| 运行时 | Python 3.13.12 / Node v24.16.0 / macOS-26.6.2 |

两次运行的代码不同一版，差异只有一处会影响 agent 能做什么：`c839524` 的沙箱只拒了
`benchmarks/coding`，而承载该目录内容的 `.git` 在它的**上一级**，于是
`git -C <monorepo> show HEAD:<case 路径>` 能读出用例的 gold 解与隐藏测试。`ee7ff7e` 把整个
checkout 都拒掉，实测 `git show`、`cat`、`grep`、`head`、`sed` 全部被拒。

**这个洞在第一次运行里没有被用到**：那一轮记录的越界尝试无一指向仓库或 `.git`（可从导出文件
逐条核对）。所以两轮仍可配对读，但**基线取 `ee7ff7e` 那一轮**，因为只有它是随仓库分发的代码
所测出来的。

集合指纹被 `configs/runs/baseline-pi.yaml` 用 `expected_case_set_fingerprint` 钉住，是本仓库
第一个这么做的 run 配置。**它证明的是这一轮跑的确实是仓库里这 45 条**；这 45 条与
`docs/BASELINE.md` 所测那批的关系，是那一页 §5.1 自己论证的（指纹并不相同：上游
`c8e47df626a689a9`，本仓库 `9149d02169845dc5`，差异来自脱敏与 annotate）。

## 2. 实验配置

| 项 | 值 |
|---|---|
| 运行配置 | [`configs/runs/baseline-pi.yaml`](../configs/runs/baseline-pi.yaml) |
| Agent / 模型配置 | [`configs/agents/pi.yaml`](../configs/agents/pi.yaml) / [`configs/models/glm52.yaml`](../configs/models/glm52.yaml) |
| 采样 | `temperature=0.0, max_tokens=8192`（**适配器实发**，见下） |
| 上下文窗口 | 128000 |
| 预算 | `max_steps=40`、`max_wall_time_s=900`、`max_attempts=1`、`first-submit` |
| 基础设施重试 | `case_retry=2`（与 `max_attempts` 不是一回事；两轮均无 `infra_error`，故未触发） |
| 并发 | `case_workers=3`（对网关的实际并发即 3） |
| 工具集 | `read, bash, edit, write, grep, find, ls`（`--tools` 严格白名单） |
| 边界 | `sandbox-exec`，**两轮 45/45 行均记录 `sandboxed: true`** |

预算值与 `configs/runs/baseline-opencode.yaml` 相同，是为了让后续 pi–opencode 对照只变
agent 一条轴。**但两边执行预算的机制不同**，见 §7.3。

**采样是实发的，不是只写在 manifest 里的。** pi 对 OpenAI 兼容端点默认**不发** temperature、
自己的 `maxTokens` 默认是 16384；而 `runner.py` 无论适配器发了什么都会把模型配置里的值盖进
manifest。适配器把两者都写进每次运行生成的 `models.json`，所以本表与实际请求一致。

## 3. 结果摘要

| 运行 | 代码 | 有效 Case | task_success_rate | 95% Wilson | 墙钟 | Token | 成本数字 |
|---|---|---:|---:|---|---:|---:|---:|
| **`prod-pi-43f69d86`（基线）** | `ee7ff7e` | **45** | **35.6%**（16/45） | [23.2%, 50.2%] | 25.1 min | 2,867,643 | 2.868 |
| `prod-pi-558ef312`（第二次观测） | `c839524` | 45 | 31.1%（14/45） | [19.5%, 45.7%] | 25.4 min | 2,273,759 | 2.274 |

模板里的 Baseline / Candidate 两行在这里是同一配置的两轮，不是对照实验：**本次建立的就是基线**。

分母口径：`task_success_rate = 成功数 / 有效 case`，有效 = 排除 `infra_error`。
两轮 **`infra_error_count` 均为 0**，所以有效 case 恒为 45，`completion_rate = 1.0`
（两轮 45 行全部 `agent_status: completed`）。

> **「成本数字」不是价钱。** `summary.json` 自己记的 `cost_rate.source` 是
> *"built-in fallback, no rate configured — this is not a price"*：未配置
> `AIBENCH_USD_PER_MTOK_*`，harness 用内置兜底的 **输入 0.5 / 输出 1.5 的算术平均 1.0**
> 乘以总 token。而本次 token 是 **92.1% 输入 / 7.9% 输出**，按同一组兜底费率分开算应是
> **1.660**，不是 2.868 —— 差 42%。两个数都能从导出文件复算（§8.2）。要真实成本请配置费率
> 后重跑，或按自己的费率用导出的 `prompt_tokens` / `completion_tokens` 自行计算。

## 4. 分项结果（均为基线轮 `prod-pi-43f69d86`）

### 4.1 按 `metadata.difficulty`（标签，非实测）

| 档 | n | 通过 | 成功率 | 95% Wilson |
|---|---:|---:|---:|---|
| easy | 14 | 3 | **21.4%** | [7.6%, 47.6%] |
| medium | 25 | 9 | **36.0%** | [20.2%, 55.5%] |
| hard | 6 | 4 | **66.7%** | [30.0%, 90.3%] |

**easy 档仍是最低的一档，方向与标签相反。** 三个区间彼此大幅重叠，hard 档只有 6 条。

对照时要小心：`docs/BASELINE.md` §4 给出的分层（easy 14.3% / medium 44.0% / hard 50.0%）是
**`bare-glm5-r1`，即 GLM-5**，不是 GLM-5.2；那一页同时记着 **`bare-glm52-r1` 恰恰不单调**
（hard 33.3% 低于 medium 36.0%）。所以「easy 标签反向」这个方向在两处都成立，
而「难度序整体单调」在任何一处都不成立。

这些标签是生成阶段的启发式估计、不是实测，在这个集合上**没有预测力**。要按难度分层报数，
得先跑 `calibrate-cases` 拿实测 `p_hat`。`metadata.tier` 45 条全部未设置（导出文件里
`tier` 恒为 `null`），所以 `stratified_by_tier` 只有一个 `unknown` 桶。

### 4.2 按语言

| 语言 | n | 通过 | 成功率 | 95% Wilson | 判分方式 |
|---|---:|---:|---:|---|---|
| python | 37 | 10 | **27.0%** | [15.4%, 43.0%] | `python -m pytest -q` |
| javascript | 8 | 6 | **75.0%** | [40.9%, 92.9%] | `node --test` |

两个区间重叠，**n=8 撑不起「pi 在 JS 上更强」**。两轮方向一致（第一轮 24.3% vs 62.5%），
所以值得在下一轮专门盯住，但它现在还不是结论。

### 4.3 harness 判定无效的 7 条

`audit-cases` 认为有 7 条不合格：6 条 `hidden_test_requires_unknowable_kwarg`（隐藏测试要求
一个在可见文件、可见测试和题面里都推断不出来的参数名，**按构造就解不出来**），
1 条 `gold_is_collection_control_file`。导出文件逐条带了 `validity_error_codes`，可自行核对。

**两轮这 7 条全部未通过。** 剔掉之后：

| 口径 | n | 通过 | 成功率 | 95% Wilson |
|---|---:|---:|---:|---|
| 全集 | 45 | 16 | 35.6% | [23.2%, 50.2%] |
| **harness 认可的子集** | **38** | **16** | **42.1%** | [27.9%, 57.8%] |

`run` 子命令**不做** `validity_ok` 过滤（过滤只在 `ablation` 里），所以单次 run 始终是 45 条。
换分母就要重新声明口径 —— 两行都写出来，避免以后被混用。

### 4.4 两轮配对

| | 条数 |
|---|---:|
| 两轮都过 | 14 |
| 两轮都不过 | 29 |
| **仅基线轮过** | **2**（`rev-10cc572e61227773`、`rev-298ab8b9d9d05d08`） |
| 仅第二次观测过 | 0 |

**基线轮严格包含另一轮**：没有任何一条从「过」翻成「不过」。两轮不一致 2/45 = 4.4%，
极差 4.4pp。`temperature: 0` 已设置，这 4.4% 是模型在贪心解码下的自身不确定性
（外加两轮沙箱范围的那一处差异，但无用例触及它）。

参照量级：`docs/BASELINE.md` 在同一集合上测到裸模型 GLM-5 的三轮极差是 **4.4pp**、
GLM-5.2 是 **8.9pp**。**pi 的两轮抖动与裸模型同量级。**

## 5. 失败分析（基线轮）

29 条失败，harness 的分类高度单一：

| 类别 | 条数 | `agent_status` | 原因 |
|---|---:|---|---|
| 模型推理失败 | 29 | `completed` | `script_exit=1` |
| 工具或执行环境失败 / 判分失败 / 超时或预算耗尽 / 基础设施失败 | **0** | —— | —— |

**没有一行是被预算停下来的**，见 §7.1。**`reward_hack` 两轮均为 0/45。**

### 5.1 这个「单一」是分类粗，不是失败真的都一样

同一批失败的 `test_pass_ratio`（隐藏测试通过比例）分布得很开：

- 29 条失败**没有一条是 0**，区间是 **0.200 – 0.857**；
- **11 条 ≥ 0.8** —— 超过三分之一的失败只差一两个测试就过了。
- 14 条通过的全部恰为 1.0。

这是导出文件里最有行动价值的一列，`失败=29` 这个计数把它整个盖住了。

### 5.2 6 条空补丁，其中 6 条**没有留下任何收尾发言**

基线轮有 6 条 `empty_patch`（agent 一个文件都没改），全部失败。它们的共同点不是「看完决定不改」：
**6 条的 `final_message` 全为空**（`final_message_empty: true`），而 `exit_code` 是 0、
`session_errors` 是 0，turn 数 3–10。第一轮同样：10 条空补丁里 9 条没有收尾发言。

也就是说，会话是**干净地结束但什么都没说也什么都没做**。这不是一次「判断无需修改」——
没有一条说过类似的话。这是一个**尚未解释的现象**，是下一轮最值得先看的样本，
但本报告不宣称知道原因。

### 5.3 越界路径计数怎么读

基线轮 `out_of_workspace_attempts` 记了 **24 次**（11 行），`tool_errors` 10 次。

**这 24 次里指向仓库或 `.git` 的是 0 次。** 逐条归类：16 次是 agent 用**自己工作区的绝对
路径**（`cd /private/var/folders/.../aibench-pi-*/workspace && ...`）—— 完全正当，是这个检测
器的假阳性；4 次是在 `/tmp` 下建临时目录做验证；1 次是 `find /`；3 次其他。

**并且要说清楚：这些不是「被拒绝」的。** 沙箱是 `(allow default)` 加一条 deny，只拒被保护的
checkout，其余文件系统照常可读可写 —— 所以 `/tmp` 的写入确实发生了，`find /` 确实跑了。
这个计数记录的是「**tool 调用里出现了工作区之外的路径**」，既不等于越权，也不等于被拦。
真正被拦的东西不会出现在这里，会出现在 `tool_errors` 里。**不要把它读作作弊计数。**

### 5.4 唯一改了测试控制文件的一条

`rev-bb519c7bf511ac8b` 写了 `conftest.py`。这**是正当的** —— 它正是那条
`gold_is_collection_control_file` 用例，参考解本身就要求改 `conftest.py`，防干扰门禁因此挂不上去。
其余 44 条没有任何一条动过测试文件（可从导出的 `files_written_count` 与 harness 的
`protected_paths` 门禁交叉核对）。

## 6. 成本与效率（基线轮）

百分位一律用**最近秩**（nearest-rank，即第 ⌈p·n⌉ 小），不是线性插值；
`numpy.percentile` 默认是后者，两者在 n=45 上会给出不同的 p90。§8.2 给了可复算的代码。

| 指标 | 值 |
|---|---:|
| 墙钟（实际经过） | 1504.6 s = **25.1 min** |
| Agent 墙钟累加 | 1.221 h（3 并发，故与上一行不同） |
| 每 case 墙钟 | p50 **76 s** · p90 200 s · max 398 s（预算 900 s） |
| 总 Token | 2,867,643（输入 2,641,508 = 92.1%，输出 226,135 = 7.9%） |
| 每 case Token | 平均 **63,725** · p50 42,991 · p90 106,090 · max 559,478 |
| 模型调用（= turn 数） | 403 · p50 9 · p90 13 · **max 18**（预算 40） |
| 工具调用（`step_count`） | 407 |
| 成本数字 | 2.868（内置兜底费率，**不是价钱**，见 §3） |

`total_wall_time_h` 是实际经过时间，`total_agent_wall_time_h` 是各 case 墙钟之和；
3 并发下后者约为前者的 2.9 倍，两个数不能混用。

**`step_count`（407）与 `max_steps`（40）不是同一个量。** 预算约束的是 **turn**（模型调用，
本轮最大 18），一个 turn 可以发多次工具调用，所以 407 与 40 不能相比。

## 7. 结论

### 7.1 两个预算都没有生效

turn 数最大 18、预算 40；每 case 墙钟最大 398 s、预算 900 s。**没有任何一行触到任一上限**
（两轮皆然）。所以本次测的是「pi 自己认为做完了为止」的表现，而不是「40 轮之内能做到多少」。
manifest 里 `budget_axis: steps`、`budget_value: max_steps=40` 如实记录了配置，
但**这个预算在这两轮里没有约束力** —— 不要把这个数读成一条预算曲线上的点。

### 7.2 两轮仍不足以给任何东西排序

`docs/BASELINE.md` §3 在这个集合上量过：同一模型同一配置重跑一次就有 2–4 条结论翻转，
**模型自身的轮间抖动与模型之间的差距同量级**。本次两轮的极差 4.4pp、不一致 2 条，
正好落在那个量级里。

因此本报告可以用来判断「pi 适配器跑通了没有、数字对不对得上、成本与耗时是多少、
抖动有多大」，**不能**用来说 pi 比 opencode 好或差、GLM-5.2 在 pi 下比在裸模型下强或弱。
那类比较至少要 3 轮重复并报轮间极差，且两侧都要重复。

### 7.3 已知的仪器差异（与 opencode 并列时必须先读）

| 项 | opencode | pi |
|---|---|---|
| 步数预算 | CLI 原生 `maxSteps`，告诉模型步数用尽、模型在预算内收尾 | **CLI 没有**；由适配器数 `turn_end` 并按进程组 kill，第 N+1 轮进行中被砍 |
| bash 命令 | 有命令名白名单（与 `tool_loop` 同一份） | **只有整工具粒度**，被保护的 checkout 之外是不受限 shell |
| 自带沙箱 | 有权限配置（**不是**文件系统边界，见 REFERENCE §12.4） | **没有，且其文档明说没有** |

第一条意味着 pi 在预算真正生效时会丢掉最后一轮的 token 与收尾机会（本次未触发，见 §7.1）。
第二条的具体含义见 §5.3：checkout 之外 pi 可以读写文件、访问网络，**网关凭据也在它的子进程
环境里**。用例集是不受信输入（`REFERENCE.md` §13），所以一条恶意用例的题面理论上可以让模型
把凭据发出去 —— 本次两轮无此迹象（越界计数逐条核对过），但这是这个适配器目前真实的残留风险，
容器隔离才是彻底的答案。

### 7.4 与 `docs/BASELINE.md` 裸模型数字的关系

`docs/BASELINE.md` §7 自己写了并列条件：同一集合、`bare_model` 适配器、`temperature=0`、
**三轮重复**，「换掉其中任何一条，得到的数就不该和本页并列」。本次换掉了适配器与轮数。
**所以本节不做通过率并列**，只记一个可复算的量级差，并注明它的口径陷阱：

| 口径 | pi（基线轮） | 裸模型 GLM-5.2（三轮均值） | 比值 |
|---|---:|---:|---:|
| **总** token / case | 63,725 | 6,312 | **10.1×** |
| **输出** token / case | 5,025 | 6,312（该协议下总量即输出量级） | **0.80×** |

**这两个数必须一起读。** pi 的 10.1 倍几乎全在输入侧：它 92.1% 的 token 是提示词，
在 405 次模型调用里被反复重发；裸模型只发一次提示词。按「产出了多少内容」算，
pi 反而略少。**单独引用 10× 会把一个协议差异说成一个能力差异。**

### 7.5 本次改动移动了 `harness_digest`

`harness_digest` 由 `src/aibench/agents/` 下全部 `*.py` 计算，新增 `pi.py` 与 `cli_sandbox.py`
使它从 `docs/BASELINE.md` 记录的 `5eeb65621cce9e42` 变为 `69d797ee`（基线轮）。这是该机制的
设计行为、不是缺陷，但后果是真的：`anchor_fingerprint` 随之改变，既有校准的 `--reuse-from`
复用资格失效；`docs/BASELINE.md` 记录的那个 digest 描述的是本次改动**之前**的树。

### 7.6 证据边界

- 两轮，且两轮代码不同一版（§1）；未做配对显著性检验（n 太小，2 条不一致）。
- 45 条全部 `bugfix`，全部 `review_status: needs_review`（无人工复核）。
- 边界是 `sandbox-exec`，macOS 专有且 Apple 已弃用；Linux 上跑同一配置会得到
  `sandboxed: false`（或被适配器拒绝），那是另一台仪器。
- 判分仍在宿主机上跑、没有容器 —— `docs/REFERENCE.md` §13 记的最大安全缺口，本次未改变。
- 模型侧无法对齐：网关与模型快照未随仓库分发，**本页绝对数值是参照，不是验收线**。

## 8. 产物

### 8.1 路径

| 产物 | 位置 |
|---|---|
| 运行配置 | [`configs/runs/baseline-pi.yaml`](../configs/runs/baseline-pi.yaml) |
| 入库数字 | [`benchmarks/ai_coding/calibrations/clean2026-pi-glm52-2runs_20260825.json`](../benchmarks/ai_coding/calibrations/clean2026-pi-glm52-2runs_20260825.json) |
| 适配器 | [`src/aibench/agents/pi.py`](../src/aibench/agents/pi.py)、[`cli_sandbox.py`](../src/aibench/agents/cli_sandbox.py) |
| 适配器文档 | [`docs/REFERENCE.md` §12.5](../docs/REFERENCE.md) |
| 原始运行目录 | `runs/AI-Coding-Assist__20260825_184557_prod-pi-43f69d86`（基线）、`runs/AI-Coding-Assist__20260825_174854_prod-pi-558ef312` |

> `runs/` 下带时间戳的目录是**本地产物，未随仓库分发**（`.gitignore`）。
> 上面两个路径在别人的 clone 里不存在，所以本报告的每一个数都同时存在于入库的
> calibration JSON 里。原始 `results.jsonl` 未发布 —— `grade.detail` 带 pytest 回溯。

### 8.2 怎么自己复算

```python
import json, math
d = json.load(open("benchmarks/ai_coding/calibrations/clean2026-pi-glm52-2runs_20260825.json"))
B = d["baseline_run"]                                   # "prod-pi-43f69d86"
O = [r["run_id"] for r in d["runs"] if r["run_id"] != B][0]
rows = d["per_case"]                                    # 45 行纯数字，每行两轮

# §3 主指标（两轮）
for run in (B, O):
    k = sum(c["runs"][run]["passed"] for c in rows)
    print(run, k, "/", len(rows))                       # 43f69d86 16/45 · 558ef312 14/45

# §4.2 语言 · §4.3 有效子集 · §4.1 难度
for lang in ("python", "javascript"):
    s = [c for c in rows if c["language"] == lang]
    print(lang, sum(c["runs"][B]["passed"] for c in s), "/", len(s))   # 10/37 · 6/8
ok = [c for c in rows if c["validity_ok"] is not False]
print("valid", sum(c["runs"][B]["passed"] for c in ok), "/", len(ok))  # 16/38
for dif in ("easy", "medium", "hard"):
    s = [c for c in rows if c["difficulty_label"] == dif]
    print(dif, sum(c["runs"][B]["passed"] for c in s), "/", len(s))    # 3/14 · 9/25 · 4/6

# §4.4 配对
print(sum(1 for c in rows if c["runs"][B]["passed"] and not c["runs"][O]["passed"]),   # 2
      sum(1 for c in rows if c["runs"][O]["passed"] and not c["runs"][B]["passed"]))   # 0

# §6 百分位 —— 最近秩，与报告一致
def pct(vals, p):
    v = sorted(vals); return v[max(0, math.ceil(p / 100 * len(v)) - 1)]
w = [c["runs"][B]["wall_time_s"] for c in rows]
print(pct(w, 50), pct(w, 90), max(w))                   # 76.42 199.82 398.04
t = [c["runs"][B]["total_tokens"] for c in rows]
print(pct(t, 50), pct(t, 90), max(t))                   # 42991 106090 559478

# §3 成本：兜底费率下，混合值 vs 按实际 92/8 拆分
pin = sum(c["runs"][B]["prompt_tokens"] for c in rows)
pout = sum(c["runs"][B]["completion_tokens"] for c in rows)
print(round((pin + pout) / 1e6 * 1.0, 3), round(pin / 1e6 * 0.5 + pout / 1e6 * 1.5, 3))  # 2.868 1.66

# §5.1 失败梯度 · §5.2 空补丁 · §7.1 预算未生效 · §2 边界
f = [c["runs"][B] for c in rows if not c["runs"][B]["passed"]]
print(len(f), min(x["test_pass_ratio"] for x in f), sum(1 for x in f if x["test_pass_ratio"] >= 0.8))
e = [c["runs"][B] for c in rows if c["runs"][B]["empty_patch"]]
print(len(e), all(x["final_message_empty"] for x in e))                  # 6 True
print(max(c["runs"][B]["turns"] for c in rows), all(c["runs"][B]["sandboxed"] for c in rows))  # 18 True
```

`runs[]` 里是 §3/§4.1/§5/§6 的汇总量与分层；`execution_identity[<run_id>]` 是每轮的版本、
预算、采样与起止时间；`cost_rate` 是 §3 那段告诫的出处。

### 8.3 重跑

```bash
cd benchmarks/coding
npm install -g @earendil-works/pi-coding-agent@0.84.3
uv run python -m aibench doctor                 # `pi` 一项须为 ok（0.84.3）
uv run python -m aibench run \
  --run-config configs/runs/baseline-pi.yaml --case-set _clean2026
```

要与本页逐条对齐，必须同时满足：同一集合（指纹 `9149d02169845dc5`，配置已钉住）、
pi **0.84.3**、`temperature=0`、`max_steps=40`、`max_wall_time_s=900`、`sandbox-exec` 可用、
代码 `ee7ff7e` 或更新。**并且要跑不止一轮** —— 单轮落在本页两轮之间的任何位置都不奇怪。
