# Coding Benchmark

从真实 AI 编程会话 trace 提取**有能力区分度**的评测用例，并在固定 case 集上替换 Agent / 模型做消融。

本目录是 Muyuan 的自包含模块（`benchmarks/coding/`）。在本目录安装依赖、跑测试和调用 CLI。`repo_root()` 解析为本目录。

| 产物 | 位置 |
|------|------|
| 实验配置 | `configs/runs/` |
| 本地运行结果 | `runs/`（gitignore，不入库） |
| 正式报告 | `reports/` |
| 已入库校准数字 | `benchmarks/ai_coding/calibrations/` |

## 它做什么

```text
真实会话 trace
    → 抽取草稿 + 回放编辑得到 (pre, post)
    → 规则筛选
    → 反向构造用例（stub = pre，参考解 = post，模型只写测试与题面）
    → 效度门禁 + 校准选题（留下能区分能力的题）
    → 在固定集合上跑 run / ablation
```

主线是**反向构造**（`generate-cases --reverse`）：缺陷来自工程师真实改过的那次编辑，而不是让模型编题。正向生成仍可用，但不是推荐路径。

随仓库分发的只有 `seed-v0`（四条 fixture，给测试用）。生产 case 集在本地生成，不入库。

## 安装

```bash
cd benchmarks/coding
uv sync --extra dev --extra grading
cp .env.example .env          # 填写 AIBENCH_DB_URL / OPENAI_*
set -a && source .env && set +a
uv run python -m aibench doctor
```

`./scripts/install-hooks.sh` 会同步 extras；嵌套在 Muyuan 时不往父仓库安装 pre-commit。

Python **3.13**（`.python-version`）。Node **≥ 22.18**（`.nvmrc` 固定 24；低于下限时 `node --test` 发现不了 TypeScript 测试却退出 0，会被判为通过）。

## 先跑通（离线）

```bash
uv run python -m aibench validate-cases --case-set seed-v0
uv run python -m aibench run --run-config tests/fixtures/configs/runs/baseline.mock.yaml \
  --case-set seed-v0
```

`audit-cases --case-set seed-v0` 报 0/4 通过是预期：这四条是最小 fixture，不是合格用例样板。

## 从 trace 建一批可用题

需要数据库与模型网关。`--require-edits` 是 `--reverse` 的前提：没有 `metadata.file_versions` 就没有 pre/post 对。

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

结构门禁只能保证「该有区分度」。要用锚点面板实测后再选题：

```bash
uv run python -m aibench calibrate-cases --case-set auto-v0 --repeats 3
uv run python -m aibench select-cases \
  --calibration runs/calibration_<ts>/calibration.json \
  --from-set auto-v0 --to-set disc-v0
```

默认淘汰：`p_hat > 0.9`、`p_hat < 0.05`、点二列相关 `< 0.15`。锚点面板见 `configs/runs/anchor-panel.yaml`，必须同时变化模型与 Agent。

`runs/` 下带时间戳的目录是本地产物，未随仓库分发。

## 消融

同一 case 集、每行只改一条轴（模型或 Agent）：

```bash
uv run python -m aibench ablation \
  --matrix configs/runs/ablation-matrix.yaml \
  --case-set disc-v0 \
  --baseline-experiment openai-compat-glm52 \
  --export-csv
```

一键流水线（抽取到消融）：

```bash
./scripts/e2e_pipeline.sh --limit 100 --max-cases 8
./scripts/e2e_pipeline.sh --dry-run    # 只用 fixture，不访问 DB/网关
```

已有集合时的单次跑测：`./scripts/run_benchmark.sh`（默认 openai_compat + GLM-5.2 + auto-v0）。

## 文档

| 文档 | 用途 |
|------|------|
| [用户手册 `docs/USER_GUIDE.md`](docs/USER_GUIDE.md) | 环境、逐步操作、读结果、故障排查 |
| [参考手册 `docs/REFERENCE.md`](docs/REFERENCE.md) | CLI、配置、Schema、门禁、产物字段 |
| [生产配置 `configs/README.md`](configs/README.md) | Agent / 模型 / Run 怎么选 |
| [文档站](docs/html/index.html) | 背景、反向构造原理、数据格式 |
| [校准数据](benchmarks/ai_coding/calibrations/README.md) | 已入库数字与复算方法 |

查参数用 `docs/REFERENCE.md`，不要用 `docs/html/reference.html`（那是设计与格式说明，不是参数手册）。

## 目录

```text
src/aibench/                 harness
tests/                       单测与 seed-v0
configs/                     生产 Agent / 模型 / Run（无 mock）
scripts/                     e2e、跑测、文档构建
docs/                        手册、参考、HTML 站
benchmarks/ai_coding/        schema、校准、本地 case 集
experiments/                 实验说明
reports/                     需提交的正式报告
runs/                        本地运行产物（gitignore）
```

## 开发

```bash
uv run ruff format --check src tests scripts && uv run ruff check src tests scripts
uv run pytest tests/ -q
uv run python -m aibench doctor
uv run python scripts/check_doc_links.py
```

`./scripts/lint.sh` 会改写源码，是修复工具，不是门禁。

Licence: see [`LICENSE`](LICENSE).

## English

This module extracts capability-discriminating coding-benchmark cases from real AI-assisted programming traces, then runs ablation on a fixed case set.

Work from `benchmarks/coding/`. Start with [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md). Parameter reference is [`docs/REFERENCE.md`](docs/REFERENCE.md). Licence: see [`LICENSE`](LICENSE).
