# pi 0.84.3 + GLM-5.2 on `_clean2026` — 运行归档

报告在 [`reports/pi-glm52-clean2026-baseline-20260825.md`](../../reports/pi-glm52-clean2026-baseline-20260825.md)，
纯数字导出在 [`calibrations/clean2026-pi-glm52-3reps_20260825.json`](../../benchmarks/ai_coding/calibrations/clean2026-pi-glm52-3reps_20260825.json)。
本目录是**这两者背后的原始运行证据**。

`runs/` 仍然不入库，也不该入库：它是 12MB 的临时产物，含 276 个缓存目录、5.4MB 工作区副本，
并且每份 manifest 里都有网关地址和本机 home 路径。入库的是**证据**，不是草稿纸。

## 四轮

| run_id | code_version | harness_digest | 通过 | 说明 |
|---|---|---|---:|---|
| `prod-pi-43f69d86` | `ee7ff7e` | `69d797ee…` | 16/45 | 三轮之一 |
| `prod-pi-5e76551f` | `e227d78` | `69d797ee…` | 14/45 | 三轮之一 |
| `prod-pi-17986236` | `e227d78` | `69d797ee…` | 13/45 | 三轮之一 |
| `prod-pi-558ef312` | `c839524` | `e830e2ea…` | 14/45 | **旁证**，沙箱修复前，另一个仪器 |

前三轮 `harness_digest` 相同，是可并列的三轮重复；第四轮不同，不算进重复计数（理由见报告 §1）。

## 目录

| 路径 | 内容 | 来源 |
|---|---|---|
| `manifests/` | 每轮的 `run_manifest.json` | harness 自动生成 |
| `summaries/` | 每轮的 `summary.json`（83 个字段） | harness 自动生成 |
| `results/` | 每轮 45 行逐例结果 | harness 自动生成 |
| `generated-reports/` | 每轮 `runs/<id>/report.md` **原样** | harness 自动生成 |
| `artifacts/` | 每例的 agent artifacts 与逐步工具调用 | `pi` 适配器写入 |
| `patches/` | **模型实际写出的补丁**，按轮一个文件 | 本目录构建时算出 |

`generated-reports/` 原样保留，是为了让读者能分清报告里哪些数字是 harness 算的、
哪些是人写分析时算的 —— 报告 §9.3 有对照表。

## 补丁是怎么算出来的，以及一个必须知道的坑

`patches/*.diff` 是把用例的 `context.files` 与运行结束时的工作区对比得到的，
**但只对比 `artifacts.files_written` 列出的文件**。

这一限制不是优化，是正确性要求：判分前 harness 会把 `grader.hidden_tests` 落到同一个工作区，
所以不加限制地 diff 会把隐藏测试算成模型写的 —— 实测那样会得到「45/45 用例都改了测试文件」，
而真实数字是 **1/45**（`rev-bb519c7bf511ac8b`，那条用例的参考解本来就要求改 `conftest.py`）。
`files_written` 是适配器在 agent 跑完、判分开始**之前**做的工作区快照差，是权威记录。

## 脱敏

四类替换，网关用的是本仓库既有的占位符约定（`docs/BASELINE.md` §5.1）：

| 原值 | 替换为 |
|---|---|
| 本机 checkout 路径 | `<repo>` / `<monorepo>` / `<home>` |
| 每例的 staging 临时目录、tmp 根 | `<staging>` / `<tmp>` |
| pytest 临时目录名里嵌的账号名 | `pytest-of-<user>` |
| 网关 `IP:端口` | `llm-gateway.example.invalid:8080` |

核对过：本目录 `deepsky` / `/Users/` / `/private/var` / 网关 IP / `OPENAI_API_KEY` 全部 **0 次命中**，
`aibench secrets-scan` 退出 0。

**没有做脱敏的一类**：`results/*.jsonl` 的 `grade.detail` 带 pytest 回溯，其中含隐藏测试源码。
`docs/BASELINE.md` 与 `calibrations/README.md` 曾以此为由不发布原始结果 —— 那条理由在这里
**不成立**：`_clean2026` 的 45 条用例**全部**已经带着 `grader.hidden_tests` 入库，回溯里出现的
测试源码在仓库里本来就是公开的。所以这里发布它，并在此写明理由，而不是沿用一条已经不适用的
禁令。

## 复算

```bash
cd benchmarks/coding
python3 - <<'PY'
import json, pathlib
A = pathlib.Path("experiments/pi-glm52-clean2026-20260825")
T = ["prod-pi-43f69d86", "prod-pi-5e76551f", "prod-pi-17986236"]
for r in T:
    rows = [json.loads(l) for l in (A/"results"/f"{r}.jsonl").read_text().splitlines() if l.strip()]
    print(r, sum(x["passed"] for x in rows), "/", len(rows))     # 16 / 14 / 13
PY
```

报告里每一个数的复算配方在报告 §9.2。
