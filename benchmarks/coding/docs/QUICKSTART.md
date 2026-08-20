# 快速上手：跑随仓库分发的 `_clean2026`

给刚 clone 完、想尽快跑出一份结果报告的人。目标只有一个：**把 `benchmarks/ai_coding/cases/_clean2026`
这 45 条用例跑一遍，拿到 `report.md`。**

想从真实会话 trace 自己造题，看 [`USER_GUIDE.md`](USER_GUIDE.md)；参数逐个查 [`REFERENCE.md`](REFERENCE.md)；
这批用例的历史成绩看 [`BASELINE.md`](BASELINE.md)。

工作目录始终是 `benchmarks/coding/`。

---

## 1. 装环境

```bash
cd benchmarks/coding
uv sync --extra dev --extra grading
uv run python -m aibench doctor
```

`doctor` 五项全 `ok` 再继续。注意 `doctor` 自己的退出码只由前三项决定，而**这三项里只有 `node` 会在跑测时真的改变结果**：

| 检查 | 要求 | 不满足会怎样 |
|---|---|---|
| `python` | 必须等于 `.python-version`（**3.13**） | 只让 `doctor` 退 1；`run` 不检查这一项 |
| `node` | **≥ 22.18**（`.nvmrc` 固定 24） | 低于下限时 `node --test` 发现不了 TypeScript 测试却退出 0。harness 现在会先拦下来把这些 case 记成 `infra_error`（`runner.py`），但**别绕过它** —— 那正是这道门存在的原因。集合里有 **5 条**隐藏测试是 `.ts`，受此影响；另外 2 条 `.mjs` 与 1 条 `.js` 不受影响 |
| `grading-env` | `configs/grading-env.yaml` 里每个包都能 import | 默认只 `[warn]` 并记进 manifest；用 `--require-grading-env` 才阻断。但包不全时用例会真的判负 |
| `opencode` / `opencode-sandbox` | 只在用 opencode adapter 时需要 | 仅告警 |

**`--extra grading` 不能省。** 只写 `uv sync --extra dev` 会把 grading 的包卸掉，判分随即失败。

**判分子进程要求 `python` 这个名字在 PATH 上**（45 条里 37 条的 `grader.command` 是 `python -m pytest -q`，
另外 8 条是 `node --test`）。`uv run` 会正确设置，所以下面所有命令都带 `uv run` 前缀；
绕过它直接调 `.venv/bin/python`，那 37 条 pytest 用例会全部 `exit=127: python: command not found`（8 条 `node --test` 的不受影响，于是你会得到一份看起来只错了一部分的报告）。

---

## 2. 先确认用例集是好的（离线，不需要任何凭据）

```bash
uv run python -m aibench validate-cases --case-set _clean2026
# OK case_set=_clean2026
```

秒级返回，**只读，不执行 case 里的任何东西** —— schema、`case_id` 唯一性，以及
`grader.command` / `setup_commands` 里有没有 shell 语法。

> **这一条和下一条的区别很重要。** 一个 case 集是别人写的数据：`grader.command` 与
> `context.workspace.setup_commands` 是命令，判分在你的机器上跑、没有容器。`validate-cases`
> 不执行它们，**`audit-cases` 会**（它要真跑一遍 stub 与参考解）。所以拿到来路不明的集合，
> 顺序是先 validate、看清楚了再 audit。随仓库分发的 `_clean2026` 已经过这一关。

```bash
uv run python -m aibench audit-cases --case-set _clean2026
# audit case_set=_clean2026 passed=38/45 failed=7 uncollectable_stub=0 uncollectable_reference=0 fingerprint=9149d02169845dc5
```

这一步会对每条用例真跑两次子进程（stub 必须失败、参考解必须通过），本机实测约 15 秒。

> **`38/45` 是预期值，不是环境坏了。** 7 条卡在两道 2026-08-17 才加进 `validity.py` 的门禁上，
> 而这批用例是那之前造的。哪 7 条、为什么、以及它对 `ablation` 分母的影响，见
> [`BASELINE.md` §5.2](BASELINE.md)。`fingerprint` 也应当正好是 `9149d02169845dc5`；
> 对不上说明用例被改过。

---

## 3. 离线跑一遍，拿到报告（仍然不需要凭据）

```bash
uv run python -m aibench run \
  --run-config tests/fixtures/configs/runs/baseline.mock.yaml \
  --case-set _clean2026
```

```text
run_dir=.../runs/AI-Coding-Assist__<时间戳>_<run_id>
success_rate=0.000 (0/45) tokens=450 cost=0.00045
```

**`0/45` 是对的。** mock adapter 不会解题，它只走完整条流水线。这一步验证的是**管道通不通**：
45 条工作区都能物化、判分器都能跑起来、报告能写出来。看这几个数：

| 字段 | 应当是 | 含义 |
|---|---|---|
| `case_count` | 45 | 集合读全了 |
| `effective_case_count` | 45 | 没有一条死在基础设施上 |
| `infra_error_count` | 0 | 同上 |
| `completion_rate` | 1.0 | 45 条都跑到了判分 |
| `case_set_fingerprint` | `9149d02169845dc5` | 跑的确实是这批用例 |

产物在 `run_dir` 下：

| 文件 | 用途 |
|---|---|
| `report.md` | 人类可读的报告，先看这个 |
| `summary.json` | 成功率、Wilson 区间、Token、成本 |
| `tables.json` | 综述表一行 |
| `results.jsonl` | 每条用例一行明细 |
| `run_manifest.json` | 配置、指纹、`code_version` —— 回答「这次跑测能不能和那次比」 |
| `cases/<case_id>/` | 每条的工作区、`workspace_manifest.json`、`result.json` |

`runs/` 在 `.gitignore` 里，**是本地产物，未随仓库分发**。

---

## 4. 用真模型跑（需要网关凭据）

到这一步才需要配置。

```bash
cp .env.example .env
# 填 OPENAI_API_KEY 与 OPENAI_BASE_URL
set -a && source .env && set +a
```

`AIBENCH_DB_URL` 只有从数据库抽会话造题时才用，跑现成用例不需要。

模型名以 `configs/models/*.yaml` 的 `model` 字段为准，`OPENAI_MODEL` 只在该字段为空时兜底 ——
否则多模型对比会静默跑成同一个模型。

### 单次跑测

```bash
uv run python -m aibench run \
  --run-config configs/runs/baseline-bare.yaml \
  --case-set _clean2026
```

产物结构与 §3 相同，只是 `success_rate` 这次有意义了。

> **这条命令跑的是 GLM-5.1**，因为 `configs/runs/baseline-bare.yaml` 里写的是
> `model_config: configs/models/glm51.yaml`。[`BASELINE.md`](BASELINE.md) 的两个模型是
> **GLM-5 与 GLM-5.2**，所以这一次的数字不能直接和基线并列。要对基线就用下面的消融，
> 或者加 `--model configs/models/glm52.yaml` 覆盖 —— 但那只对齐了模型这一条轴，**仍然是单轮**，
> 而基线的口径是三轮（理由见下一节第一条）。单轮的数不要和基线并列。

**凭据没配好会怎样。** 全部 45 条会记成 `infra_error`，`effective_case_count` 归零，
命令退出码 1。终端会先刷 45 行 `[retry] case <id> infra_error attempt 1/2: missing API key env: …`，
然后是这两行：

```text
run_dir=<run_dir>
FAILED: no case executed. All 45 died on infrastructure (45 infra errors) — this is not a 0% pass rate. Check credentials, the gateway and `aibench doctor`; see <run_dir>/report.md.
```

产物照样会写盘，而且每一处都说得出「没测到」：

| 位置 | 值 |
|---|---|
| `summary.json` 的 `success_rate` | `null` |
| `summary.json` 的 `effective_case_count` / `infra_error_count` | `0` / `45` |
| `report.md` 的「主指标值」「成功率」「成功率 95% CI」 | `-` |
| `tables.json` 的 `overview_row["主指标值"]` | `"-"`；`general_row` 的「主指标值」「成功率」「完成率」都是 `null` |
| 消融里这一行的成功率、相对基线收益、McNemar 的 p 值与「显著」 | 全是 `-` 或空 |

**「全部 case 都死在基础设施上」和「一条都没做对」是两件事**，所以这里不写 `0.0%`。
判断一次 run 有没有测到东西，看 `effective_case_count`。

### 复现 `BASELINE.md` 的那次测量

```bash
uv run python -m aibench ablation \
  --matrix configs/runs/ablation-widest-gap.yaml \
  --export-csv
```

矩阵已经写死了基线用的全部条件：`case_set: _clean2026`、`bare_model` 适配器、
GLM-5 与 GLM-5.2 各 3 轮、`allow_invalid_cases: true`、`parallel: 3`。**其中三条改了就对不上基线**：

- 少于 3 轮得不出结论。同一个对比单跑一次报过 `p = 0.0312`「显著」，跑三次是 `p = 0.167` 不显著。
- 去掉 `allow_invalid_cases` 会让 §2 的那 7 条被静默剔除，跑成 n=38，与基线的 n=45 无法并列。
- 换掉 `bare_model` 就换掉了被测对象 —— 脚手架贡献的方差比模型之间的差距还大。

结果看 `runs/ablation_<时间戳>/ablation_report.md`；配对显著性用同一集合上的 McNemar。
某一行如果没测出任何东西（比如那一个模型的凭据坏了），它的成功率与相对基线收益都是空的，
不是 `0.0%` 和一个负的 pp 值；`ablation_report.md` 另有一段警告点名这些行。
拿到的数怎么和基线对齐、哪些能对齐哪些不能，见 [`BASELINE.md` §7](BASELINE.md)。

---

## 5. 会撞上的几件事

**`secrets-scan --case-set _clean2026` 退出码是 2。**
9 条 findings 全部是误报（JS 的 `password: newPassword`、`Bearer token` 测试夹具、
一句中文注释）。明细见 [`BASELINE.md` §5.3](BASELINE.md)。数据是对的，不要因为这个去改用例。

**`aibench-secrets-scan` 这个 pre-commit hook 在本仓库布局下不会被装上。**
`./scripts/install-hooks.sh` 只在模块自己就是 git 根时才 `pre-commit install`；本模块嵌套在 muyuan
仓库里，脚本会打印 `Skipped pre-commit install: this module is nested in ...` 并给出手动安装命令。
如果你照那条命令装了它，`rev-809ac0ba498403fa.json` 与 `rev-bb519c7bf511ac8b.json`（就是上面
那 9 条误报所在的两个文件）会因为退出码 2 提交不了。真要改它们就用 `git commit --no-verify`，
并自己确认改动里没有真凭证。

**`audit-cases --annotate` 会原地改写用例文件。**
`_clean2026` 已入库，所以 `git diff` 就能看出它改了什么，`git checkout` 就能还原。
其它集合没有这个退路，跑之前先 `cp -a`。

**别用 `--case-set` 指向 `runs/` 下的目录。** 那里是运行产物，不是用例集。

**结果表有很多空列是正常的。** 本 harness 没有的数据源保持 `null`。

---

## 6. 下一步

| 想做什么 | 去哪 |
|---|---|
| 看这批用例的历史成绩与口径 | [`BASELINE.md`](BASELINE.md) |
| 从自己的会话 trace 造题 | [`USER_GUIDE.md`](USER_GUIDE.md) §3.2 |
| 查某个参数的默认值 | [`REFERENCE.md`](REFERENCE.md) §8 |
| 选 Agent / 模型 / Run 配置 | [`configs/README.md`](../configs/README.md) |
| 了解反向构造的原理 | [文档站](html/index.html) |
