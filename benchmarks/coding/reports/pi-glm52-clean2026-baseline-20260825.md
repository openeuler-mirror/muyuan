# pi + GLM-5.2 在 `_clean2026` 上的基线（三轮）

**基线是三轮重复，不是一个数：35.6% / 31.1% / 28.9%，轮间极差 6.7pp。**
多数票（≥2/3）33.3%，三轮合计 43/135 = 31.9%。45 条，全部 `bugfix`，三轮 `infra_error` 均为 0。

三轮跑在**同一个仪器**上（`harness_digest 69d797ee4b05df67`），这是
`docs/BASELINE.md` 要求的重复次数与口径 —— 也就是说，**别人现在可以拿自己的三轮来和这一页比**。
另有第四轮 `prod-pi-558ef312`（31.1%）跑在沙箱修复前的代码上，是**另一个仪器**，
本页把它记下来但不算进三轮，理由见 §1。

**每一个比例、计数与分布**都能从随仓库分发的
[`clean2026-pi-glm52-3reps_20260825.json`](../benchmarks/ai_coding/calibrations/clean2026-pi-glm52-3reps_20260825.json)
算出来（45 条 × 4 轮逐例纯数字），复算配方见 §8.2。少数几处是**逐条看原始运行目录**得到的、
导出文件只带计数——§5.2 的 `exit_code`、§5.3 的归类，本文逐处标注。
原始 `results.jsonl` 未发布 —— 它的 `grade.detail` 带 pytest 回溯。

---

## 1. 实验概览

| 项目 | 内容 |
|---|---|
| 实验名称 | `prod-pi` |
| Benchmark | `AI-Coding-Assist` |
| Case 集合 | `_clean2026`（随仓库分发），45 条，全部 `task_type: bugfix` |
| 集合指纹 | `9149d02169845dc5`，四轮的声明值与实测值全部一致 |
| 被测对象 | `pi` adapter v1.0.0 驱动 pi CLI **0.84.3** |
| 模型 | GLM-5.2（OpenAI 兼容网关） |
| **协议** | **同一配置三轮重复**，`harness_digest 69d797ee4b05df67` |
| 三轮 | `prod-pi-43f69d86`（`ee7ff7e`）、`prod-pi-5e76551f`（`e227d78`）、`prod-pi-17986236`（`e227d78`） |
| 旁证一轮 | `prod-pi-558ef312`（`c839524`，harness `e830e2ea`） |
| 运行时间 | 2026-08-25 17:48 – 22:11，三轮合计墙钟 76.4 min |
| 运行时 | Python 3.13.12 / Node v24.16.0 / macOS-26.6.2 |

三轮的 `code_version` 是 `ee7ff7e` 与 `e227d78` 两个提交，但 **`harness_digest` 完全相同**
（`e227d78` 相对 `ee7ff7e` 只改了文档与报告，没动 `src/`）。本仓库判定「是不是同一台仪器」
用的就是这个摘要，所以三轮同仪器。

**第四轮为什么不算进来。** `prod-pi-558ef312` 跑在 `c839524` 上，那一版的沙箱只拒了
`benchmarks/coding`，而承载该目录内容的 `.git` 在它的**上一级**，于是
`git -C <monorepo> show HEAD:<case 路径>` 能读出用例的 gold 解与隐藏测试。`ee7ff7e` 把整个
checkout 都拒掉。**那一轮没有用例用过这个洞**（20 次越界尝试无一指向仓库或 `.git`；
逐条看过原始运行目录），所以它的数字本身可信 —— 但它的 `harness_digest` 不同，
按本仓库自己的口径就是另一台仪器，不该混进重复计数里。

集合指纹被 `configs/runs/baseline-pi.yaml` 用 `expected_case_set_fingerprint` 钉住，是本仓库
第一个这么做的 run 配置。**它证明的是每一轮跑的确实是仓库里这 45 条**；这 45 条与
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
| 基础设施重试 | `case_retry=2`（与 `max_attempts` 不是一回事；四轮均无 `infra_error`，故未触发） |
| 并发 | `case_workers=3`（对网关的实际并发即 3） |
| 工具集 | `read, bash, edit, write, grep, find, ls`（`--tools` 严格白名单） |
| 边界 | `sandbox-exec`，**四轮 180/180 行全部记录 `sandboxed: true`** |

预算值与 `configs/runs/baseline-opencode.yaml` 相同，是为了让后续 pi–opencode 对照只变
agent 一条轴。**但两边执行预算的机制不同**，见 §7.3。

**采样是实发的，不是只写在 manifest 里的。** pi 对 OpenAI 兼容端点默认**不发** temperature、
自己的 `maxTokens` 默认是 16384；而 `runner.py` 无论适配器发了什么都会把模型配置里的值盖进
manifest。适配器把两者都写进每次运行生成的 `models.json`，所以本表与实际请求一致。

## 3. 结果摘要

| 运行 | 代码 | 通过 | task_success_rate | 95% Wilson | 墙钟 | Token | 成本数字 |
|---|---|---:|---:|---|---:|---:|---:|
| `prod-pi-43f69d86` | `ee7ff7e` | 16/45 | **35.6%** | [23.2%, 50.2%] | 25.1 min | 2,867,643 | 2.868 |
| `prod-pi-5e76551f` | `e227d78` | 14/45 | **31.1%** | [19.5%, 45.7%] | 29.2 min | 2,091,630 | 2.092 |
| `prod-pi-17986236` | `e227d78` | 13/45 | **28.9%** | [17.7%, 43.4%] | 22.2 min | 2,499,392 | 2.499 |
| **三轮合计 / 极差** | — | **43/135** | **31.9% · 极差 6.7pp** | — | 76.4 min | 7,458,665 | 7.459 |
| **多数票（≥2/3）** | — | **15/45** | **33.3%** | [21.4%, 47.9%] | — | — | — |
| *`prod-pi-558ef312`（旁证，另一仪器）* | `c839524` | *14/45* | *31.1%* | *[19.5%, 45.7%]* | *25.4 min* | *2,273,759* | *2.274* |

**报哪个数？** 报三个，外加极差。要一个单一数字时用**多数票 33.3%**（它对单轮抖动不敏感），
不要用某一轮。三轮合计的 43/135 只是描述性的 —— 135 次试验不是 135 条独立用例，
不能拿去算区间。

分母口径：`task_success_rate = 成功数 / 有效 case`，有效 = 排除 `infra_error`。
**四轮 `infra_error_count` 全为 0**，所以有效 case 恒为 45，`completion_rate` 恒为 1.0
（180 行全部 `agent_status: completed`）。

> **「成本数字」不是价钱。** `summary.json` 自己记的 `cost_rate.source` 是
> *"built-in fallback, no rate configured — this is not a price"*：未配置
> `AIBENCH_USD_PER_MTOK_*`，harness 用内置兜底的 **输入 0.5 / 输出 1.5 的算术平均 1.0**
> 乘以总 token。而本次 token 是 **91.0% 输入 / 9.0% 输出**，按同一组兜底费率分开算，
> 三轮合计应是 **4.402** 而不是 7.459 —— 差 41%。两个数都能从导出文件复算（§8.2）。

## 4. 分项结果

每格给出三轮的逐轮值，再给多数票。**只看一轮的分项会被抖动带偏**，§4.2 是最直接的例子。

### 4.1 按 `metadata.difficulty`（标签，非实测）

| 档 | n | 三轮逐轮 | 多数票 | 95% Wilson |
|---|---:|---|---:|---|
| easy | 14 | 3/14 · 2/14 · 2/14 | **2/14 = 14.3%** | [4.0%, 39.9%] |
| medium | 25 | 9/25 · 8/25 · 9/25 | **9/25 = 36.0%** | [20.2%, 55.5%] |
| hard | 6 | 4/6 · 4/6 · 2/6 | **4/6 = 66.7%** | [30.0%, 90.3%] |

**easy 档三轮都是最低的一档，方向与标签相反。** 这是本页里唯一一个三轮一致、
且与 `docs/BASELINE.md` 在裸模型上所测方向相同的分项结论。

对照时要小心：`docs/BASELINE.md` §4 给出的分层（easy 14.3% / medium 44.0% / hard 50.0%）是
**`bare-glm5-r1`，即 GLM-5**，不是 GLM-5.2；那一页同时记着 **`bare-glm52-r1` 恰恰不单调**
（hard 33.3% 低于 medium 36.0%）。本页的 hard 档三轮 4/4/2，也谈不上稳。
所以「easy 标签反向」成立，「难度序整体单调」不成立。

这些标签是生成阶段的启发式估计、不是实测。要按难度分层报数，得先跑 `calibrate-cases` 拿实测
`p_hat`。`metadata.tier` 45 条全部未设置（导出文件里 `tier` 恒为 `null`）。

### 4.2 按语言 —— 一个「单轮会骗人」的现成例子

| 语言 | n | 三轮逐轮 | 多数票 | 95% Wilson |
|---|---:|---|---:|---|
| python | 37 | 10/37 · 10/37 · 8/37 | **10/37 = 27.0%** | [15.4%, 43.0%] |
| javascript | 8 | 6/8 · **4/8** · 5/8 | **5/8 = 62.5%** | [30.6%, 86.3%] |

第一轮的 JS 是 6/8 = 75%，第二轮掉到 4/8 = 50%。**n=8 上一条用例就是 12.5 个百分点。**
多数票 62.5% 与 python 的 27.0% 区间大幅重叠。方向三轮一致（JS 每轮都高于 python），
但**幅度完全不可信**，而且这 8 条是同一批 `node --test` 用例、来源同质。
记在这里是作为下一轮的观察项，不是结论。

### 4.3 harness 判定无效的 7 条

`audit-cases` 认为有 7 条不合格：6 条 `hidden_test_requires_unknowable_kwarg`（隐藏测试要求
一个在可见文件、可见测试和题面里都推断不出来的参数名，**按构造就解不出来**），
1 条 `gold_is_collection_control_file`。导出文件逐条带了 `validity_error_codes`。

**四轮里这 7 条一次都没通过（0/7 × 4 轮）。** 这是「按构造解不出来」最直接的实证。剔掉之后：

| 口径 | n | 三轮逐轮 | 多数票 | 95% Wilson |
|---|---:|---|---:|---|
| 全集 | 45 | 16 · 14 · 13 | 15/45 = 33.3% | [21.4%, 47.9%] |
| **harness 认可的子集** | **38** | 16 · 14 · 13 | **15/38 = 39.5%** | [25.6%, 55.3%] |

两行的分子相同 —— 因为通过的用例全部落在有效子集里。`run` 子命令**不做** `validity_ok`
过滤（过滤只在 `ablation` 里），所以单次 run 始终是 45 条；换分母就要重新声明口径。

### 4.4 三轮的稳定性 —— 本页最该记住的一节

| | 条数 | 占比 |
|---|---:|---:|
| 三轮都过 | 12 | 26.7% |
| 三轮都不过 | 29 | 64.4% |
| **三轮内变过号** | **4** | **8.9%** |

不稳定的 4 条：`rev-1b77e6e910b9452e`、`rev-298ab8b9d9d05d08`、`rev-74ba9c286a152c50`、
`rev-b8152a1f71487e75`。

**8.9% 这个数值与 `docs/BASELINE.md` 在同一集合上测到的裸模型逐例翻转率完全相同**
（GLM-5 4/45、GLM-5.2 4/45，均为 8.9%）。`temperature: 0` 已设置，所以这是模型在贪心解码下
的自身不确定性，**换了一个完全不同的脚手架之后仍是同一个量级**。

三轮两两配对 McNemar，**没有一对显著**：

| 对比 | 仅前者过 (b) | 仅后者过 (c) | 不一致 | p |
|---|---:|---:|---:|---:|
| `43f69d86` vs `5e76551f` | 2 | 0 | 2 | 0.5000 |
| `43f69d86` vs `17986236` | 3 | 0 | 3 | 0.2500 |
| `5e76551f` vs `17986236` | 2 | 1 | 3 | 1.0000 |

**同一配置的自己和自己比都不显著**，这正是 §7.2 那句话的量化版本：任何比这个差距更小的
「提升」，用这个集合、这个轮数都测不出来。

## 5. 失败分析

三轮失败数 29 / 31 / 32，harness 的分类每一轮都只有一个桶：`模型推理失败`、
`agent_status: completed`、`script_exit=1`。**四轮都没有一行是超时、判分失败或基础设施失败。**
`reward_hack` 四轮均为 0。

### 5.1 「单一」是分类粗，不是失败真的都一样

三轮的 `test_pass_ratio`（隐藏测试通过比例）分布高度一致：

| 轮 | 失败数 | ratio 区间 | =0 | ≥0.8 |
|---|---:|---|---:|---:|
| `43f69d86` | 29 | 0.200 – 0.857 | 0 | 11 |
| `5e76551f` | 31 | 0.200 – 0.857 | 0 | 12 |
| `17986236` | 32 | 0.200 – 0.857 | 0 | 12 |

**三轮都没有一条失败是 0 分，且每轮都有 11–12 条（约三分之一）只差一两个测试就过了。**
通过的用例三轮全部恰为 1.0。这是导出文件里最有行动价值的一列，
而「失败 = 29/31/32」这个计数把它整个盖住。

### 5.2 空补丁：6 / 9 / 7 条，**每一轮都是全部没有收尾发言**

`empty_patch`（agent 一个文件都没改）三轮分别 6、9、7 条，全部失败。共同点不是「看完决定不改」：
**三轮的空补丁行，`final_message` 100% 为空**（`final_message_empty: true`），
而 `exit_code` 是 0、`session_errors` 是 0。第四轮同样（10 条里 9 条）。

也就是说，会话**干净地结束但什么都没说也什么都没做**。没有一条说过「无需修改」之类的话。
四轮一致地出现、且比例不低（13%–20%），说明这不是偶发 —— 但本报告**不宣称知道原因**，
它是下一步最值得先查的东西。

### 5.3 越界路径计数怎么读

三轮 `out_of_workspace_attempts` 分别 24 / 21 / 15 次，`tool_errors` 10 / 11 / 11。

**这些不是「被拒绝」的次数。** 沙箱是 `(allow default)` 加一条 deny，只拒被保护的 checkout，
其余文件系统照常可读可写 —— 所以 `/tmp` 的写入确实发生了，`find /` 确实跑了。这个计数记录的是
「tool 调用里出现了工作区之外的路径」，既不等于越权，也不等于被拦；真正被拦的会进
`tool_errors`。**不要把它读作作弊计数。**

以第一轮的 24 次为例逐条归类（逐条看过原始运行目录；导出文件只带计数）：13 次是 agent 用
**自己工作区的绝对路径**，是检测器的假阳性；4 次在 `/tmp` 下建临时目录做验证；1 次 `find /`；
4 次其他；**3 次走进了工作区的上一级**——见下。

### 5.3.1 那 3 次值得单独说：agent 读到了约束它自己的东西

`rev-9fbb2b58ef14ed55` 用掉 7 个 turn 中的 3 个做了这件事，全部成功（`tool_errors: 0`）：

```
cd <staging> && ls -la && find . -name "*.py" | head -50
cd <staging> && find pi-agent -type f | head -50 && echo ---SANDBOX--- && cat sandbox.sb
cd <staging> && cat pi-agent/settings.json; cat pi-agent/models.json | head -30
```

它读到了**约束自己的 seatbelt 配置**，以及适配器每次运行生成的 `settings.json` 与
`models.json` —— 后者含网关地址（API key 不在里面，那是一个 `$AIBENCH_PI_API_KEY` 变量名，
见 §7.3）。**staging 目录不在被保护的 checkout 里，所以沙箱不拦它。**

这不影响任何一轮的数字：它没有拿到任何用例的答案（gold 解与隐藏测试在 checkout 里，被拒），
而且这条用例本身是空补丁、没有通过。但它是这个检测器存在的理由的真实例子。
适配器应当把生成的配置放到工作区够不着的地方，或把 staging 的兄弟目录一并拒掉 ——
**已知未修**，见 §7.6。

### 5.4 唯一改了测试控制文件的一条

`rev-bb519c7bf511ac8b` 写了 `conftest.py`。这**是正当的** —— 它正是那条
`gold_is_collection_control_file` 用例，参考解本身就要求改 `conftest.py`，防干扰门禁因此挂不上去。
其余 44 条四轮里没有任何一条动过测试文件。

## 6. 成本与效率

百分位一律用**最近秩**（nearest-rank，即第 ⌈p·n⌉ 小），不是线性插值；
`numpy.percentile` 默认是后者，两者在 n=45 上会给出不同的 p90。§8.2 给了可复算的代码。

| 指标 | `43f69d86` | `5e76551f` | `17986236` | 三轮 |
|---|---:|---:|---:|---:|
| 墙钟（实际经过） | 25.1 min | 29.2 min | 22.2 min | **76.4 min** |
| 每 case 墙钟 p50 / p90 / max | 76 / 200 / 398 s | 96 / 226 / 392 s | 71 / 168 / 335 s | 预算 900 s |
| 每 case Token | 63,725 | 46,481 | 55,542 | **平均 55,249** |
| 输入占比 | 92.1% | 88.9% | 91.5% | **91.0%** |
| 模型调用（= turn 数） | 403 | 371 | 379 | 1,153 |
| turn p50 / max | 9 / **18** | 8 / **15** | 8 / **21** | 预算 40 |
| 成本数字（混合费率） | 2.868 | 2.092 | 2.499 | **7.459** |
| 按实际拆分算 | 1.660 | 1.279 | 1.463 | **4.402** |

每 case token 在三轮之间从 46k 到 64k、相差 37%，而通过率只差 6.7pp ——
**token 消耗比通过率抖得厉害得多**，做成本预算时按上界算。

## 7. 结论

### 7.1 三轮里两个预算都没有生效过

turn 数三轮最大 18 / 15 / 21，预算 40；每 case 墙钟最大 398 / 392 / 335 s，预算 900 s。
**180 行里没有一行触到任一上限。**

所以本页测的是「pi 自己认为做完了为止」的表现，而不是「40 轮之内能做到多少」。
manifest 里 `budget_axis: steps`、`budget_value: max_steps=40` 如实记录了配置，
但**这个预算在这四轮里没有约束力** —— 不要把这些数读成一条预算曲线上的点。

### 7.2 现在可以拿来比了，但能比出什么是有下限的

三轮重复 + 报极差，是 `docs/BASELINE.md` 要求的口径，本页满足了。
**所以别人可以拿自己的三轮来和这一页并列**（同集合、同适配器、同采样、同预算的前提下）。

但 §4.4 给出了这个集合的分辨率下限：同一配置自己跟自己比，不一致 2–3 条、p 值 0.25–1.00，
**没有一对显著**。换句话说，**任何小于 3 条用例（≈6.7pp）的差距，用这个集合、三轮，
测不出来**。要检出更小的差距，需要更多用例或更多轮次 ——
`plan-sample-size` 子命令就是算这个的。

本页**不能**用来说 pi 比 opencode 好或差、GLM-5.2 在 pi 下比在裸模型下强或弱：
那需要另一侧也跑三轮，且只变一条轴。

### 7.3 已知的仪器差异（与 opencode 并列时必须先读）

| 项 | opencode | pi |
|---|---|---|
| 步数预算 | CLI 原生 `maxSteps`，告诉模型步数用尽、模型在预算内收尾 | **CLI 没有**；由适配器数 `turn_end` 并按进程组 kill，第 N+1 轮进行中被砍 |
| bash 命令 | 有命令名白名单（与 `tool_loop` 同一份） | **只有整工具粒度**，被保护的 checkout 之外是不受限 shell |
| 自带沙箱 | 有权限配置（**不是**文件系统边界，见 REFERENCE §12.4） | **没有，且其文档明说没有** |

第一条意味着 pi 在预算真正生效时会丢掉最后一轮的 token 与收尾机会（四轮均未触发，见 §7.1）。
第二条的具体含义见 §5.3：checkout 之外 pi 可以读写文件、访问网络，**网关凭据也在它的子进程
环境里**。用例集是不受信输入（`REFERENCE.md` §13），所以一条恶意用例的题面理论上可以让模型
把凭据发出去 —— 四轮无此迹象，但这是这个适配器目前真实的残留风险，容器隔离才是彻底的答案。

### 7.4 与 `docs/BASELINE.md` 裸模型数字的关系

轮数这条对上了（都是三轮），但**适配器不同**——`docs/BASELINE.md` §7 的并列条件要求
`bare_model`。所以本节仍**不做通过率并列**，只记三个可复算的量级：

| 口径 | pi（三轮均值） | 裸模型 GLM-5.2（三轮均值） | 比值 |
|---|---:|---:|---:|
| **总** token / case | 55,249 | 6,312 | **8.8×** |
| **输出** token / case | 4,982 | **未发布**（那批只导出了 `total_tokens`，无输入/输出拆分） | 无法计算 |
| **逐例翻转率** | 4/45 = **8.9%** | 4/45 = **8.9%** | **相同** |

**前两行必须一起读。** pi 的 8.8 倍几乎全在输入侧：91.0% 的 token 是提示词，在 1,153 次模型
调用里被反复重发；裸模型只发一次提示词。裸模型那批**没有导出输入/输出拆分**，所以
「谁产出的内容更多」**算不出来**——能确定的只是 pi 的输出量是每 case 4,982 token，
与裸模型的每 case 总量 6,312 同一量级。**单独引用 8.8× 会把协议差异说成能力差异。**

第三行是本页最结实的一处对照：**换掉整个脚手架，逐例翻转率仍是 8.9%。**
它支持 `docs/BASELINE.md` 的核心论断——在这个集合上，噪声底与模型间差距同量级——
并说明那个噪声底主要来自模型本身，而不是某一个脚手架。

### 7.5 本次改动移动了 `harness_digest`

`harness_digest` 由 `src/aibench/agents/` 下全部 `*.py` 计算，新增 `pi.py` 与 `cli_sandbox.py`
使它从 `docs/BASELINE.md` 记录的 `5eeb65621cce9e42` 变为 `69d797ee`。这是该机制的设计行为、
不是缺陷，但后果是真的：`anchor_fingerprint` 随之改变，既有校准的 `--reuse-from` 复用资格失效；
`docs/BASELINE.md` 记录的那个 digest 描述的是本次改动**之前**的树。

### 7.6 证据边界

- 三轮同仪器 + 一轮旁证；45 条全部 `bugfix`，全部 `review_status: needs_review`（无人工复核）。
- 分辨率下限见 §7.2：小于约 3 条用例的差距测不出来。
- 边界是 `sandbox-exec`，macOS 专有且 Apple 已弃用；Linux 上跑同一配置会得到
  `sandboxed: false`（或被适配器拒绝），那是另一台仪器。
- 判分仍在宿主机上跑、没有容器 —— `docs/REFERENCE.md` §13 记的最大安全缺口，本次未改变。
- **staging 目录不在沙箱的保护范围内**，所以 agent 能读到适配器生成的 `models.json` /
  `settings.json` 与 seatbelt 配置本身（§5.3.1 有一条真的读了）。不影响用例答案的保密，
  但应当修：把生成的配置移出工作区可达范围，或把 staging 的兄弟目录一并拒掉。**已知未修。**
- 模型侧无法对齐：网关与模型快照未随仓库分发，**本页绝对数值是参照，不是验收线**。

## 8. 产物

### 8.1 路径

| 产物 | 位置 |
|---|---|
| 运行配置 | [`configs/runs/baseline-pi.yaml`](../configs/runs/baseline-pi.yaml) |
| 入库数字 | [`benchmarks/ai_coding/calibrations/clean2026-pi-glm52-3reps_20260825.json`](../benchmarks/ai_coding/calibrations/clean2026-pi-glm52-3reps_20260825.json) |
| 适配器 | [`src/aibench/agents/pi.py`](../src/aibench/agents/pi.py)、[`cli_sandbox.py`](../src/aibench/agents/cli_sandbox.py) |
| 适配器文档 | [`docs/REFERENCE.md` §12.5](../docs/REFERENCE.md) |
| 原始运行目录 | `runs/AI-Coding-Assist__20260825_184557_prod-pi-43f69d86`、`..._211856_prod-pi-5e76551f`、`..._214839_prod-pi-17986236`、`..._174854_prod-pi-558ef312` |

> `runs/` 下带时间戳的目录是**本地产物，未随仓库分发**（`.gitignore`）。
> 上面四个路径在别人的 clone 里不存在，所以本报告的每一个比例、计数与分布都同时存在于入库的
> calibration JSON 里；标注为「逐条看过原始运行目录」的几处除外（§5.2 的 `exit_code`、
> §5.3 的归类）。原始 `results.jsonl` 未发布 —— `grade.detail` 带 pytest 回溯。

### 8.2 怎么自己复算

```python
import json, math
d = json.load(open("benchmarks/ai_coding/calibrations/clean2026-pi-glm52-3reps_20260825.json"))
T = d["baseline_runs"]                     # 三轮同仪器
rows = d["per_case"]                       # 45 行，每行四轮
p = lambda k, c: c["runs"][k]["passed"]

# §3 三轮 + 多数票
print([sum(p(k, c) for c in rows) for k in T])                       # [16, 14, 13]
maj = {c["case_id"]: sum(p(k, c) for k in T) >= 2 for c in rows}
print(sum(maj.values()))                                             # 15

# §4.4 稳定性
print(sum(1 for c in rows if all(p(k, c) for k in T)),               # 12  三轮都过
      sum(1 for c in rows if not any(p(k, c) for k in T)),           # 29  三轮都不过
      sum(1 for c in rows if len({p(k, c) for k in T}) > 1))         # 4   变过号

# §4.1 / §4.2 / §4.3 分项（逐轮与多数票）
for key, vals in (("difficulty_label", ("easy", "medium", "hard")),
                  ("language", ("python", "javascript"))):
    for v in vals:
        s = [c for c in rows if c[key] == v]
        print(v, [sum(p(k, c) for c in s) for k in T], sum(maj[c["case_id"]] for c in s), len(s))
ok = [c for c in rows if c["validity_ok"] is not False]
print(len(ok), [sum(p(k, c) for c in ok) for k in T], sum(maj[c["case_id"]] for c in ok))  # 38 [16,14,13] 15

# §6 百分位（最近秩）与预算
def pct(v, q):
    v = sorted(v); return v[max(0, math.ceil(q / 100 * len(v)) - 1)]
for k in T:
    w = [c["runs"][k]["wall_time_s"] for c in rows]
    print(k, round(pct(w, 50)), round(pct(w, 90)), round(max(w)),
          max(c["runs"][k]["turns"] for c in rows))     # 全部 < 900s 与 40 turn

# §3 成本：混合费率 vs 按实际拆分
pin = sum(c["runs"][k]["prompt_tokens"] for k in T for c in rows)
pout = sum(c["runs"][k]["completion_tokens"] for k in T for c in rows)
print(round((pin + pout) / 1e6, 3), round(pin / 1e6 * .5 + pout / 1e6 * 1.5, 3))   # 7.459 4.402

# §5.1 失败梯度 · §5.2 空补丁全体沉默 · §2 边界
for k in T:
    f = [c["runs"][k] for c in rows if not p(k, c)]
    e = [c["runs"][k] for c in rows if c["runs"][k]["empty_patch"]]
    print(len(f), sum(1 for x in f if x["test_pass_ratio"] >= 0.8),
          len(e), all(x["final_message_empty"] for x in e),
          all(c["runs"][k]["sandboxed"] for c in rows))
```

`runs[]` 是逐轮汇总与分层；`execution_identity[<run_id>]` 是每轮的版本、预算、采样与起止时间
（`harness_digest` 就在这里，三轮相同、旁证那轮不同）；`cost_rate` 是 §3 那段告诫的出处。

### 8.3 重跑

```bash
cd benchmarks/coding
npm install -g @earendil-works/pi-coding-agent@0.84.3
uv run python -m aibench doctor                 # `pi` 一项须为 ok（0.84.3）
for i in 1 2 3; do
  uv run python -m aibench run \
    --run-config configs/runs/baseline-pi.yaml --case-set _clean2026
done
```

要与本页并列，必须同时满足：同一集合（指纹 `9149d02169845dc5`，配置已钉住）、
pi **0.84.3**、`temperature=0`、`max_steps=40`、`max_wall_time_s=900`、`sandbox-exec` 可用、
**三轮重复并报轮间极差**、且三轮的 `harness_digest` 相同。
模型侧无法对齐 —— 网关与模型快照未随仓库分发，所以**本页绝对数值是参照，不是验收线**。
