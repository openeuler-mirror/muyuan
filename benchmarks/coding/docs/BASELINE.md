# 基线

`_clean2026`（45 条）上唯一一次成规模的历史测量：**2026-08-14，裸模型协议，GLM-5 与 GLM-5.2 各 3 轮**。
数字来自模块上游仓库的 `runs/ablation_20260814_111227/`。**`runs/` 下的路径是本地产物，未随仓库分发**，
所以这里把该次测量按纯数字导出并入库：

[`benchmarks/ai_coding/calibrations/clean2026-bare-2models-3reps_20260814.json`](../benchmarks/ai_coding/calibrations/clean2026-bare-2models-3reps_20260814.json)

**§2 §3 §4 的每一个数都能从那个文件算出来**，配方在 §6。§1 引用的几个数字来自另一批实验，
只存在于 `benchmarks/ai_coding/calibrations/README.md` 的正文里，用它们自己的口径读（见那一节的标注）。
用例本体在 `benchmarks/ai_coding/cases/_clean2026/`，随仓库分发，所以这是本模块**唯一一份可以逐条复现的基线**。

---

## 1. 测量条件

| 项 | 值 |
|---|---|
| Case 集合 | `_clean2026`，45 条，全部 `task_type: bugfix` |
| 矩阵 | [`configs/runs/ablation-widest-gap.yaml`](../configs/runs/ablation-widest-gap.yaml) |
| Agent | `bare_model` v1.0.0 —— 一次调用，模型回一段围栏代码，无 JSON 信封、无多步循环、无工具调用 |
| 模型 | GLM-5 与 GLM-5.2，各 3 轮 |
| 采样 | `temperature=0.0, max_tokens=8192` |
| 预算 | `model_calls=1, max_wall_time_s=600`；`max_attempts=1`、`max_steps=1`、`first-submit` |
| 基线实验 | `bare-glm52-r1` |
| harness | `code_version=4372c41-dirty`、`harness_digest=5eeb65621cce9e42`、`dependency_digest=d071d90b22f83b0a` |
| 运行时 | Python 3.13.12 / Node v24.16.0 |

**`code_version` 是 `4372c41-dirty`** —— 这批数字跑在一个有未提交改动的工作树上，那个代码状态无法精确复现。这不影响 §2 的数字本身（它们是实测），但意味着「同一份 harness 重跑」这件事做不到逐字节对齐。

选 `bare_model` 而不是工具循环、以及三轮重复，理由写在矩阵文件的注释里。
那两条理由引用的数字**出自另一批实验**，不是本页的测量：31 条 `reverse-v1` 用例、
Deepseek-V4-Flash 对 GLM-5.1、`tool_loop` 适配器 —— 同一模型轮间极差 12.9pp、
裸模型下 0.0pp；同一个对比单跑一次报 `p = 0.0312`「显著」，跑三轮是 `p = 0.167` 不显著。
出处是 [`calibrations/README.md`](../benchmarks/ai_coding/calibrations/README.md) 的正文，
**那两个 p 值没有逐例数据可复算**，只能当作当初做这个设计决定时的依据来读。

顺带说明一个看起来矛盾的地方：上面引的「裸模型 0.0pp」是 GLM-5.1 在那 31 条上的结果，
而本页 §2 在同一个适配器上测到 GLM-5 极差 4.4pp、GLM-5.2 极差 8.9pp。**换了模型和用例集，
裸模型并不自动等于零抖动。** 「脚手架是主要噪声源」这个方向仍成立，但 0.0pp 不是这个适配器的属性。

---

## 2. 主结果

`task_success_rate` = 成功数 / 有效 case（有效 = 排除 `infra_error`）。六次运行 `infra_error_count` 均为 0，
`completion_rate` 均为 1.0，`empty_patch_count` 均为 0，所以分母恒为 45。

| run_id | 模型 | 通过 | 成功率 | 95% Wilson | 总 Token | 估算成本 | 墙钟(h) |
|---|---|---:|---:|---|---:|---:|---:|
| `bare-glm5-r1` | GLM-5 | 16/45 | **35.6%** | [23.2%, 50.2%] | 219,628 | 0.2196 | 1.000 |
| `bare-glm5-r2` | GLM-5 | 18/45 | **40.0%** | [27.0%, 54.5%] | 233,036 | 0.2330 | 1.159 |
| `bare-glm5-r3` | GLM-5 | 16/45 | **35.6%** | [23.2%, 50.2%] | 216,293 | 0.2163 | 0.974 |
| `bare-glm52-r1` | GLM-5.2 | 13/45 | **28.9%** | [17.7%, 43.4%] | 285,733 | 0.2857 | 0.815 |
| `bare-glm52-r2` | GLM-5.2 | 15/45 | **33.3%** | [21.4%, 47.9%] | 284,724 | 0.2847 | 0.819 |
| `bare-glm52-r3` | GLM-5.2 | 17/45 | **37.8%** | [25.1%, 52.4%] | 281,636 | 0.2816 | 0.751 |

轮间极差：GLM-5 **4.4pp**（35.6–40.0），GLM-5.2 **8.9pp**（28.9–37.8）。
`total_cost` 是按 `AIBENCH_USD_PER_MTOK_*` 默认费率估的，不是账单。

---

## 3. 这批用例区分不出这两个模型

配对 McNemar，全部相对基线 `bare-glm52-r1`，**五个对比无一显著**：

| 对比 | 仅基线过 (b) | 仅候选过 (c) | 不一致 | p | 显著 |
|---|---:|---:|---:|---:|---|
| vs `bare-glm5-r1` | 0 | 3 | 3 | 0.2500 | 否 |
| vs `bare-glm5-r2` | 0 | 5 | 5 | 0.0625 | 否 |
| vs `bare-glm5-r3` | 1 | 4 | 5 | 0.3750 | 否 |
| vs `bare-glm52-r2` | 0 | 2 | 2 | 0.5000 | 否 |
| vs `bare-glm52-r3` | 0 | 4 | 4 | 0.1250 | 否 |

注意最后两行是 **GLM-5.2 和它自己**：同一模型同一配置，重跑一次就有 2–4 条结论翻转。
**模型自身的轮间抖动与模型之间的差距同量级。**

按每模型三轮多数票合并后：

| | 条数 | 占比 |
|---|---:|---:|
| 两个模型都过 | 15 | 33.3% |
| 两个模型都不过 | 27 | 60.0% |
| **仅 GLM-5 过** | **3** | **6.7%** |
| 仅 GLM-5.2 过 | 0 | 0.0% |

**有区分力的只有 3 条。** 逐 case 三轮翻转率：GLM-5 **4/45 = 8.9%**，GLM-5.2 **4/45 = 8.9%**。
`temperature: 0` 已设置，这 8.9% 是模型自身在贪心解码下的不确定性。

8.9% 与 6.7% **不是同一个统计量** —— 前者是「同一模型三轮内结论变过号的题数」，
后者是「多数票下两模型判定不同的题数」，一个量不稳定性、一个量区分力。放在一起只说明一件事：
**4 条 vs 3 条，同量级，且只差一条题。** 想拿它当「噪声压过信号」的定量结论，样本量远远不够。

更极端的口径：45 条里 **25 条六次运行全不过**、**12 条六次运行全过**，
即只有 8 条在任何一次运行里变过号。

> **这 25 条里有 7 条是本 harness 判定为无效的用例**（§5.2 的那 7 条，全部落在「六次全不过」里）。
> 其中 6 条的失败码是 `hidden_test_requires_unknowable_kwarg` —— 隐藏测试要求一个在可见文件、
> 可见测试和题面里都推断不出来的参数名，**按构造就解不出来**，与模型能力无关。
> 它们恒定落进「都不过」那一格，直接稀释配对检验的不一致对。剔掉这 7 条后的 18 条才是
> 「有效用例里无人能解」的口径。

**结论：这份基线可以用来判断「跑通了没有、数字对不对得上」，不能用来给这两个模型排序。**
任何模型比较至少 3 次重复并报轮间极差；单次运行不足以支撑任何排名。

---

## 4. `difficulty` 标签与实测通过率反向

`bare-glm5-r1` 的分层：

| `metadata.difficulty` | n | 通过 | 成功率 | 95% Wilson |
|---|---:|---:|---:|---|
| easy | 14 | 2 | **14.3%** | [4.0%, 39.9%] |
| medium | 25 | 11 | **44.0%** | [26.7%, 62.9%] |
| hard | 6 | 3 | **50.0%** | [18.8%, 81.2%] |

六轮里**每一轮 easy 都是最低的一档**；easy < medium < hard 的完整单调序只在五轮成立，
`bare-glm52-r1` 的 hard(33.3%) 低于 medium(36.0%)。hard 档只有 6 条、CI `[18.8%, 81.2%]`，
所以能说的是「easy 标签反向」，不是「难度序整体倒转」。

换成 §5.2 的 38 条有效子集重算 `bare-glm5-r1`：easy 15.4% / medium 52.4% / hard 75.0% —— 方向不变。

这些标签是生成阶段的启发式估计，**不是实测**，
在这个集合上没有预测力。要按难度分层报数，得先跑 `calibrate-cases` 拿实测 `p_hat`，
不要用 `metadata.difficulty`。`metadata.tier` 45 条全部未设置（`tier_requested` 是 `T2`），
所以 `stratified_by_tier` 只有一个 `unknown` 桶。

失败构成（`bare-glm5-r1`，29 条失败）：`模型推理失败` 28、`测试未执行` 1；
原因分布 `script_exit=1` ×28、`script_exit=2` ×1。

---

## 5. 与本仓库这份用例的差异 —— 读数前必看

### 5.1 集合指纹变了，因为用例被脱敏过

| | 指纹 |
|---|---|
| 上游测出这批数字时的集合 | `c8e47df626a689a9` |
| **本仓库 `benchmarks/ai_coding/cases/_clean2026/`** | **`9149d02169845dc5`** |

两者不等，原因只有一个：45 条里有 5 条含真实标识，入库前做了一致替换。

| 原值 | 替换为 | 涉及文件 |
|---|---|---|
| 企业微信机器人 webhook key（UUID） | `00000000-0000-0000-0000-000000000000` | 3 |
| 真实个人邮箱 | `benchmark-user@example.com` | 4 |
| 自建推理服务公网 `IP:端口` | `llm-gateway.example.invalid:8080` | 4 |
| 真实代码托管账号名 | `example-owner` | 1 |

替换在 `context.files`、`grader.gold_files`、`grader.hidden_tests`、`prompt` 上一致生效，
所以可见源码与隐藏测试不会对同一个字面量产生分歧。

**没有任何一个文件与上游逐字节相同** —— 入库前跑过 `audit-cases --annotate`，它给 45 条都写了
`metadata.difficulty_scale` 与 `metadata.validity_unverified` 两个新键。逐字段核对下来，全部差异是：

| 字段 | 变动条数 | 来自 |
|---|---:|---|
| `metadata.difficulty_scale`、`metadata.validity_unverified` | 45 | annotate 新增（上游那批 annotate 时代码里还没有这两个键） |
| `metadata.validity_ok`、`metadata.validity_issues` | 7 | annotate 写回本仓库的审计判定，见 §5.2 |
| `metadata.fingerprint` | 5 | 内容变了，恰好是被脱敏的那 5 个文件 |
| `context` / `grader` / `prompt` 等非 `metadata` 字段 | **0** | —— |

也就是说：**用例内容本身只有上表那 4 条替换被改过**，其余全是 annotate 写的元数据。
把 4 条替换反向应用回去，45 个文件的非 `metadata` 部分与上游完全一致；
**未脱敏副本在本仓库 harness 上算出的集合指纹正是 `c8e47df626a689a9`** —— 入库的这批就是基线所测的那批。

### 5.2 本仓库的效度门禁比测出这批数字时更严：38/45

```bash
uv run python -m aibench audit-cases --case-set _clean2026
# audit case_set=_clean2026 passed=38/45 failed=7
```

上游文档记录这批是 `45/45`。差异**不是脱敏造成的** —— 未脱敏副本在本仓库跑出的同样是 38/45，
且失败的是同样 7 条。本仓库的 `validity.py` 多了两道当时没有的门：

| case_id | 失败门禁 |
|---|---|
| `rev-05e88429bf55fa4d` `rev-863adbcb81f3b56f` `rev-8a469aa591bca490` `rev-9f27702b16ac2ace` `rev-a60dac9e85e808bf` `rev-fef49ee571e4af8c` | `hidden_test_requires_unknowable_kwarg` —— 隐藏测试传了一个在可见文件、可见测试和题面里都找不到的参数名 |
| `rev-bb519c7bf511ac8b` | `gold_is_collection_control_file` —— 参考解要改 `conftest.py`，防干扰门禁没法挂上去 |

这 7 条的 `metadata.validity_ok` 是 `false`，`metadata.validity_issues` 各有两条 —— 上表那条 error，加一条 `tier_missing` warn。
那条 warn **45 条都有**（`metadata.tier` 全部未设置，见 §4），不影响 `validity_ok`，读 `git diff` 时会看到它。

**后果是具体的**：`ablation` 默认剔除
`validity_ok: false` 的 case，所以不加开关跑出来的是 **n=38**，与本页的 n=45 对不上，而且不会报错。
`configs/runs/ablation-widest-gap.yaml` 因此写了 `allow_invalid_cases: true`。
`run` 子命令不做这个过滤，单次 run 始终是 45 条。

想在 38 条上重测也是合理的 —— 那是本 harness 认为合格的子集 —— 但那不是本页的基线，
换分母就要重新声明口径。

### 5.3 已知残留，没有处理

| 项 | 情况 |
|---|---|
| `metadata.source_session_id` | 45/45 保留。是上游生产对话库的会话 ID，离开那个库没有意义，同时是用例唯一的溯源线索，所以没有脱敏。 |
| `context.notes` 的 `db-<id>` | 同上。 |
| URL 被写坏：`https://` → `http/workspace/`，`http://` → `htt/workspace/` | 上游脱敏脚本的 bug：它的路径正则把 `s://` / `p://` 当成 Windows 盘符，于是吃掉了每一个协议头。**不能修**——坏字符串已经写进 8 条隐藏测试的断言里（如 `assert url.startswith('http/workspace/')`、`assert conftest.TEST_ISSUE_URL == "http/workspace/atomgit.com/..."`），修它会当场判负这些用例，并让基线失效。 |
| `secrets-scan` 报 9 条 | `uv run python -m aibench secrets-scan --case-set _clean2026` 退出码 2。9 条经逐条核对全部是误报。其中 `password_assign` 与 `api_key_assign` 正是 [`REFERENCE.md` §8.15](REFERENCE.md) 自己记为「已知精度问题（未修）」的那两条规则；`bearer` 未被该节点名，但同属「引用凭证的代码被判成写死的凭证」这一类。明细：`password_assign` 4 条是 JS 的 `password: newPassword`，`bearer` 3 条是 `Bearer token` 测试夹具，`api_key_assign` 2 条是 `conftest.py` 里的一句中文注释。数据没有改。 |
| `review_status` | 45/45 是 `needs_review`，没有一条经过人工复核。 |

---

## 6. 怎么自己复算

导出文件的结构：`runs[]`（六次运行的逐项指标）、`pairwise_comparisons[]`（五个 McNemar 对比）、
`per_case_passed`（`{case_id: {run_id: 通过与否}}`，45 × 6 个布尔值）。

```python
import json

d = json.load(open("benchmarks/ai_coding/calibrations/clean2026-bare-2models-3reps_20260814.json"))
pc = d["per_case_passed"]
g5 = [f"bare-glm5-r{i}" for i in (1, 2, 3)]
g52 = [f"bare-glm52-r{i}" for i in (1, 2, 3)]

# §3 的翻转率：同一模型三轮里结论变过号的 case
flips = lambda rs: sum(1 for c in pc if len({pc[c][r] for r in rs}) > 1)
print(flips(g5), flips(g52))                                    # 4 4

# §3 的多数票四格表
maj = lambda rs: {c: sum(pc[c][r] for r in rs) >= 2 for c in pc}
a, b = maj(g5), maj(g52)
print(sum(a[c] and b[c] for c in pc),                           # 15 都过
      sum(not a[c] and not b[c] for c in pc),                   # 27 都不过
      sum(a[c] and not b[c] for c in pc),                       # 3  仅 GLM-5
      sum(b[c] and not a[c] for c in pc))                       # 0  仅 GLM-5.2
```

§2 的成功率、CI、Token 直接读 `runs[]`；§4 的分层读 `runs[i]["stratified_by_difficulty"]`。
导出用的是白名单：只取列出的数值与标识字段，因此文件里没有源码、没有 pytest 回溯、
没有网关地址、没有本机路径。原始 `results.jsonl` **未发布** —— 它的 `grade.detail` 带 pytest 回溯。

---

## 7. 重跑

见 [`QUICKSTART.md`](QUICKSTART.md)。要与本页逐条对齐，必须同时满足：同一集合（45 条）、
`bare_model` 适配器、`temperature=0`、三轮重复、`allow_invalid_cases: true`。
换掉其中任何一条，得到的数就不该和本页并列。

模型侧无法完全对齐：上游用的网关与模型快照未随仓库分发，`GLM-5` / `GLM-5.2` 指向什么由
`configs/models/*.yaml` 与你自己的 `OPENAI_BASE_URL` 决定。**因此本页的绝对数值是参照，不是验收线**；
可以直接对齐的是结构性结论 —— 六次运行分母恒为 45、失败集中在 `script_exit=1`、
区分度只有个位数条、噪声底与模型间差距同量级。
