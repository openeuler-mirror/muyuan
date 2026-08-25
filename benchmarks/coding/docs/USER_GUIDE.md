# 用户手册

从真实会话 trace 做出一批有区分度的用例，再在固定集合上做消融。参数级说明见 [`REFERENCE.md`](REFERENCE.md)。

工作目录是 `benchmarks/coding/`。

---

## 1. 流程

```text
MySQL / JSON 会话
        │
        ▼
  extract-from-db / extract-cases     草稿 + 回放得到 (pre, post)
        │
        ▼
  filter-drafts                       去掉非编程 / 不可重建的会话
        │
        ▼
  generate-cases --reverse            stub=pre，参考解=post，模型写测试与题面
        │
        ▼
  validate-cases → audit-cases        schema + 效度门禁
        │
        ▼
  calibrate-cases → select-cases      锚点实测，留下有区分度的题
        │
        ▼
  run / ablation                      固定集合上换 Agent / 模型
        │
        ▼
  runs/.../summary.json + ablation_report.md
```

| 名称 | 含义 |
|------|------|
| **draft** | 从会话抽出的草稿，未必要可跑 |
| **auto-v0** | 自动候选集（`needs_review`），不是正式发布集 |
| **disc-v0** | 校准后按区分度选出的集合（命名约定，可自定） |
| **prod-v0** | 人工 `promote` 后的正式集 |

生产默认无 mock：Agent = `openai_compat`，模型 = `GLM-5.2`。mock 只在 `tests/fixtures/configs/` 与 `--dry-run`。

只想跑随仓库分发的 `_clean2026`（45 条）拿一份报告、不打算自己造题的，直接看 [`QUICKSTART.md`](QUICKSTART.md)。

---

## 2. 环境

```bash
cd benchmarks/coding
uv sync --extra dev --extra grading
cp .env.example .env
set -a && source .env && set +a
uv run python -m aibench doctor
```

| 场景 | 需要 |
|------|------|
| 从 MySQL 抽会话 | `AIBENCH_DB_URL` |
| 生成 case / 真 Agent 跑测 | `OPENAI_API_KEY` + `OPENAI_BASE_URL`（建议再设 `OPENAI_MODEL`） |
| 离线 dry-run / seed-v0 | 可不填 |

完整变量表见 [`REFERENCE.md` §6](REFERENCE.md#6-环境变量完整参考)。不要提交 `.env`。

模型名以模型 YAML 的 `model` 字段为准；`OPENAI_MODEL` 只在该字段为空时兜底。否则多模型消融会静默跑成同一个模型。

Python 3.13；Node ≥ 22.18（仓库 `.nvmrc` 为 24）。低于 22.18 时 TypeScript 测试会被漏跑并判通过。

---

## 3. 操作

### 3.1 离线冒烟

```bash
uv run python -m aibench validate-cases --case-set seed-v0
uv run python -m aibench run --run-config tests/fixtures/configs/runs/baseline.mock.yaml \
  --case-set seed-v0
./scripts/e2e_pipeline.sh --dry-run
```

`seed-v0` 只有四条 fixture。`audit-cases` 对它报 0/4 通过是预期。

### 3.2 从数据库建用例

```bash
uv run python -m aibench extract-from-db \
  --output-dir benchmarks/ai_coding/cases/drafts-from-db \
  --limit 100 --max-cases 30 --require-gold --require-edits

uv run python -m aibench filter-drafts \
  --input-dir benchmarks/ai_coding/cases/drafts-from-db \
  --output-dir benchmarks/ai_coding/cases/drafts-kept

uv run python -m aibench generate-cases \
  --input-dir benchmarks/ai_coding/cases/drafts-kept \
  --output-dir benchmarks/ai_coding/cases/auto-v0 \
  --reverse --resume --max-cases 8 --audit --secrets-scan

uv run python -m aibench validate-cases --case-set auto-v0
uv run python -m aibench audit-cases --case-set auto-v0 --annotate --fail-on-error
```

`--require-edits` 保证草稿带 `metadata.file_versions`，否则 `--reverse` 一条都建不出来。

只要建题、先不要消融时，不要跑完整 `e2e_pipeline.sh`（脚本末尾会跑 ablation）。

### 3.3 校准与选题

```bash
uv run python -m aibench calibrate-cases --case-set auto-v0 --repeats 3 --parallel 3 --workers 5
uv run python -m aibench select-cases \
  --calibration runs/calibration_<ts>/calibration.json \
  --from-set auto-v0 --to-set disc-v0
```

锚点必须同时跨越模型与 Agent，且含一个明显弱的锚点，否则 `spread` 恒为 0，校准会把题都判成无区分度。默认面板：`configs/runs/anchor-panel.yaml`。

### 3.4 单次跑测与消融

```bash
./scripts/run_benchmark.sh
# 或
uv run python -m aibench run --run-config configs/runs/baseline.yaml --case-set disc-v0

uv run python -m aibench ablation \
  --matrix configs/runs/ablation-matrix.yaml \
  --case-set disc-v0 \
  --baseline-experiment openai-compat-glm52 \
  --export-csv
```

矩阵每一行只改一条轴，否则 McNemar 配对差异无法归因。矩阵里的 `parallel:` 会被读到（`ablation --parallel` 的 argparse 默认值是 `None`），命令行只在要覆盖它时才传。`configs/runs/ablation-matrix.yaml` 自己写的是 `parallel: 1`，所以上面这条命令本来就是串行跑。

### 3.5 一键流水线

```bash
./scripts/e2e_pipeline.sh --limit 100 --max-cases 8
./scripts/e2e_pipeline.sh --limit 100 --max-cases 8 --calibrate --repeats 3
```

### 3.6 发布正式集

```bash
uv run python -m aibench promote --from-set auto-v0 --to-set prod-v0 \
  --require-audit --case-id <id>
```

---

## 4. `--limit` 与 `--max-cases`

| 参数 | CLI 默认 | e2e 默认 | 含义 |
|------|----------|----------|------|
| `--limit` | 抽库 300 | 100 | DB 最多扫描多少行 |
| `--max-cases`（抽库） | 30 | max-cases×3 | 最多写多少草稿 |
| `--max-cases`（生成） | 50 | 8 | 最多写多少最终 case |

不传参 = 用默认，不是无限扫库。完整默认值见 [`REFERENCE.md` §4.3](REFERENCE.md#43-limit-与-max-cases)。

---

## 5. 配置怎么选

| 需求 | 配置 |
|------|------|
| 默认单次生产跑测 | `configs/runs/baseline.yaml` |
| 多步工具 Agent | `configs/runs/baseline-tool-loop.yaml` |
| 真实编码 agent（opencode） | `configs/runs/baseline-opencode.yaml` |
| 真实编码 agent（pi） | `configs/runs/baseline-pi.yaml`（需 `npm i -g @earendil-works/pi-coding-agent`） |
| 比模型（尽量去掉脚手架） | `configs/agents/bare_model.yaml` + `runs/baseline-bare.yaml` |
| 对照消融 | `configs/runs/ablation-matrix.yaml` |
| 换模型 | `configs/models/glm51.yaml` / `qwen37.yaml` 等 |
| 包外部 CLI | `configs/agents/shell.yaml`（必须填 `command_template`） |
| 采样 pass@k | `configs/runs/passk.yaml` + `models/glm52-sampling.yaml` |
| 单测 / dry-run | 仅 `tests/fixtures/configs/` |

`temperature: 0` 下 k 次采样是同一个样本，pass@k 恒等于 pass@1。说明见 [`configs/README.md`](../configs/README.md) 与 [`REFERENCE.md` §7](REFERENCE.md#7-生产配置体系-configs)。

---

## 6. 命令一览

入口：`uv run python -m aibench <cmd>`。

| 命令 | 何时用 |
|------|--------|
| `extract-from-db` / `extract-cases` | 从 MySQL 或 JSON 建草稿 |
| `filter-drafts` | 去掉运维/闲聊/非编程会话 |
| `generate-cases --reverse` | 草稿变成可机评 case |
| `validate-cases` | Schema 校验 |
| `audit-cases` | stub 必 fail、参考解必 pass、污染检测 |
| `calibrate-cases` | 锚点面板测 p_hat / 区分度 |
| `select-cases` | 按校准结果选题 |
| `run` | 一组 Agent/模型跑一遍 |
| `ablation` | 多组配置横向对比 |
| `promote` | 人工发布到正式集 |
| `export-bundle` | 导出可分发的集合 |
| `secrets-scan` | 扫密钥 |
| `doctor` | 本机能否产出可比测量 |
| `plan-sample-size` | 按实测不一致率反推题量 |

| 脚本 | 作用 |
|------|------|
| `scripts/run_benchmark.sh` | 生产单次跑测 |
| `scripts/e2e_pipeline.sh` | DB→case→消融；`--dry-run` 仅 fixture |

每个 flag 的默认值见 [`REFERENCE.md` §8](REFERENCE.md#8-cli-命令完整参考)。

---

## 7. 怎么读结果

`runs/` 下带时间戳的目录是本地产物，未随仓库分发。

### 单次 run

`runs/AI-Coding-Assist__<时间>_<run_id>/`

| 文件 | 用途 |
|------|------|
| `summary.json` | 成功率、Wilson CI、Token、成本 |
| `tables.json` | 综述表一行 |
| `report.md` | 人类可读 |
| `results.jsonl` | 每 case 明细 |
| `run_manifest.json` | 配置、指纹、`code_version` |

主指标：`task_success_rate`（成功数 / 有效 case；有效 = 排除 `infra_error`）。

### 消融

`runs/ablation_<时间>/ablation_report.md` 是多行对比表，汇报时优先看这份。显著性用同一 case 集上的 McNemar 配对检验。

字段映射见 [`REFERENCE.md` §16](REFERENCE.md#16-运行产物与结果表映射)。

---

## 8. 效度门禁（够用的部分）

保证指标差尽量来自 Agent/模型，而不是坏题或答案泄漏。完整列表见 [`REFERENCE.md` §14](REFERENCE.md#14-科学效度scientific-validity定义门禁与逻辑)。

| 级别 | 例子 | 是否阻断 `validity_ok` |
|------|------|------------------------|
| **error** | stub 必须 fail；参考解必须 pass；gold/key line 不得已在 context；测试不得 grep 源码 | 是 |
| **warn** | prompt 大代码块、weak_grader | 否 |

```bash
uv run python -m aibench audit-cases --case-set auto-v0 --annotate --fail-on-error
```

---

## 9. 常见问题

**auto-v0 为空？**  
先确认 extract → filter → generate 都跑过；`--require-edits` 是否带上；`.env` 的 DB/API 是否可用。

**只想生成 case？**  
不要跑完整 `e2e_pipeline.sh`。按 §3.2。

**生成阶段全失败？**  
检查 `OPENAI_*`。失败会回退启发式（多为 gold，区分度弱），不要把这种题当主集。

**校准把所有题都丢掉？**  
检查锚点面板是否同时变了模型与 Agent，以及弱锚点是否真的弱。

**结果表很多空列？**  
正常。本 harness 没有的数据源保持 `null`。

**接外部 CLI？**  
用 `shell` adapter，填好 `command_template`，或实现新 adapter 并注册。

**要多少题才能下结论？**

```bash
uv run python -m aibench plan-sample-size --delta 10 \
  --from-ablation runs/ablation_<ts>/ablation_summary.json
```

---

## 10. 文档索引

| 文档 | 说明 |
|------|------|
| [README](../README.md) | 模块入口 |
| [QUICKSTART.md](QUICKSTART.md) | 跑随仓库分发的 `_clean2026` 并拿到报告 |
| [BASELINE.md](BASELINE.md) | `_clean2026` 上的历史测量与复算方法 |
| [REFERENCE.md](REFERENCE.md) | CLI、配置、Schema、产物 |
| [configs/README.md](../configs/README.md) | 生产配置怎么选 |
| [文档站](html/index.html) | 背景与数据格式 |
| [校准 README](../benchmarks/ai_coding/calibrations/README.md) | 已入库数字如何复算 |

---

## 11. 命令速查

```bash
uv sync --extra dev --extra grading && set -a && source .env && set +a

uv run python -m aibench extract-from-db \
  --output-dir benchmarks/ai_coding/cases/drafts-from-db \
  --limit 100 --max-cases 30 --require-gold --require-edits
uv run python -m aibench filter-drafts \
  --input-dir benchmarks/ai_coding/cases/drafts-from-db \
  --output-dir benchmarks/ai_coding/cases/drafts-kept
uv run python -m aibench generate-cases \
  --input-dir benchmarks/ai_coding/cases/drafts-kept \
  --output-dir benchmarks/ai_coding/cases/auto-v0 \
  --reverse --resume --max-cases 8 --audit --secrets-scan
uv run python -m aibench validate-cases --case-set auto-v0

./scripts/run_benchmark.sh
uv run python -m aibench ablation --matrix configs/runs/ablation-matrix.yaml --case-set disc-v0 --export-csv
./scripts/e2e_pipeline.sh --limit 100 --max-cases 8
```
