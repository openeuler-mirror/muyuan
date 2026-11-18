# AHE 快速入门

AHE 仅依赖 Python ≥ 3.10 标准库与进化模型的 LLM API，无需额外安装。以下
四种使用方式可任选其一。

## 方式一：作为 Pi 插件使用（推荐）

```bash
# 一次性注册（写入 Pi settings.json 的 packages 列表）
python3 plugins/ahe/install.py --pi    # 或 bash plugins/ahe/install.sh --pi
```

此后以任意常规方式启动 Pi（仅需不带 `--no-extensions`），即可直接输入：

```text
/ahe 优化harness, harness路径为/path/to/harness,
    eval任务为/path/to/benchmark里面的项目（无 benchmark 时改写 提示词为任务描述）,
    指标为token降30%, 时间降20%, 准确率不变, 3轮
```

evolve / eval / debugger 自动复用 Pi 的 API 配置（环境变量或同一份
`.env`），无需手动编写 manifest 或传入 API 参数。可选段：
`eval任务为<benchmark路径>里面的<项目>`（benchmark 只读、真实 evaluator
自动接线）、`manifest路径为...`、`评测器为...`、`holdout评测器为...`、
`任务根目录为...`、`自动发现adapter`，完整语法见
[README.md](README.md#安装与使用)。取消注册：
`python3 plugins/ahe/install.py --pi --uninstall`（或
`bash plugins/ahe/uninstall.sh --pi`）。

## 方式二：作为 Codex 自定义命令使用

```bash
# 一次性安装（渲染模板写入 ~/.codex/prompts/ahe.md，CODEX_HOME 可指定其他位置）
python3 plugins/ahe/install.py --codex   # 或 bash plugins/ahe/install.sh --codex
```

此后启动 Codex，支持自定义 prompt 的版本即可直接输入（语法与 Pi 完全
一致；较新版本 codex 若不再加载 prompts 目录，可粘贴渲染后的模板内容
或直接以 CLI 驱动引擎）：

```text
/ahe 优化harness, harness路径为/path/to/harness,
    eval任务为/path/to/benchmark里面的项目（无 benchmark 时改写 提示词为任务描述）,
    指标为token降30%, 时间降20%, 准确率不变, 3轮
```

evolve / eval / debugger 的 API 配置自动回退读取 Codex 的
`~/.codex/auth.json` + `config.toml`（亦支持环境变量 / `AHE_ENV_FILE`）。
卸载：`python3 plugins/ahe/install.py --codex --uninstall`（或
`bash plugins/ahe/uninstall.sh --codex`）。

## 方式三：优化单个 prompt 文件（零配置）

```bash
export LLM_BASE_URL=http://<gateway>/v1 LLM_API_KEY=<key> LLM_MODEL=<model>

python3 plugins/ahe/ahe.py my-prompt.txt \
  --task "回答问题" \
  --test "2+2=?|4" "法国首都?|巴黎" \
  --rounds 3
```

AHE 将文件内容作为 system prompt，由 LLM 回答每道测试题并进行子串匹配，
每轮进化 prompt；仅当准确率不降且 token/时间改善时保留。此方式无需
manifest 与评测脚本。

## 方式四：优化任意多文件 harness 目录

### 第一步：运行前预检（只读，确认安装状态与运行方式）

```bash
# 报告安装入口、依赖状态（node_modules/.venv/运行探针）与运行方式
python3 plugins/ahe/ahe.py /path/to/harness --preflight

# 未就绪（缺依赖/探针失败）时，先从源目录安装，再重新预检
python3 plugins/ahe/ahe.py /path/to/harness --install-harness
```

### 第二步：生成 manifest（或基于模板修改）

```bash
# 自动发现：识别 prompt/tool/skill/middleware 等可变文件，排除任务代码与评测器
python3 plugins/ahe/ahe.py /path/to/harness \
  --discover-adapter \
  --task-root /path/to/task \          # 可选：任务仓库保持只读
  --task "优化 harness 完成任务" \
  --eval-cmd "python3 /path/to/eval.py" \
  --validation-cmd "python -m compileall ." \
  --output-manifest /tmp/harness.manifest.json
```

亦可直接复制最小模板 `templates/manifest.example.json` 并修改路径：

```json
{
  "name": "my-harness",
  "version": "1.0",
  "description": "任意 harness：模板、策略、运行时均可注册",
  "patch_mode": "structured",
  "baseline_frozen": true,
  "adapter": {"name": "generic"},
  "components": [
    {"id": "prompts", "kind": "prompt", "path": "prompts", "mutable": true},
    {"id": "runtime", "kind": "runtime_core", "path": "src", "mutable": true}
  ],
  "validation": {
    "command": ["python", "-m", "compileall", "-q", "src"],
    "timeout_s": 600
  },
  "evaluation": {"metrics": ["accuracy", "tokens", "time_s"]}
}
```

只有 `mutable: true` 的组件可被进化模型修改，其余文件只读。

### 第三步：提供评测命令（或由 AHE 自动生成）

评测器从环境变量读取输入，stdout 最后一行输出 JSON。最小实现（复制
`templates/eval_example.py` 并修改评分逻辑）：

```python
import json, os, subprocess, sys, time
cand = os.environ["AHE_HARNESS_ROOT"]      # 候选 harness 目录（评测对象）
t0 = time.time()
r = subprocess.run([sys.executable, "run_benchmark.py"], cwd=cand,
                   capture_output=True, text=True, timeout=3600)
accuracy = parse_accuracy(r.stdout)        # 自定义评分逻辑
print(json.dumps({"accuracy": accuracy, "tokens": 0, "time_s": time.time() - t0}))
```

若不希望手写评测器，可由 AHE 根据任务自动生成（同时将 keep 主指标写入
manifest）：

```bash
python3 plugins/ahe/ahe.py /path/to/harness \
  --manifest /tmp/harness.manifest.json \
  --task "优化 harness 完成任务" \
  --eval-task /path/to/benchmark --eval-project <目标项目> \
  --generate-evaluator     # 生成 ahe_evaluator.py + AHE-OBJECTIVE，审阅后作为 --eval-cmd 使用
```

### 第四步：启动优化循环

```bash
python3 plugins/ahe/ahe.py /path/to/harness \
  --manifest /tmp/harness.manifest.json \
  --task "准确率不降，token 与时间下降" \
  --eval-cmd "python3 /path/to/eval.py" \
  --rounds 5
```

可先仅运行 baseline 以验证链路（此步骤不需要 LLM API）：

```bash
python3 plugins/ahe/ahe.py /path/to/harness \
  --manifest /tmp/harness.manifest.json \
  --task "验证链路" \
  --eval-cmd "python3 /path/to/eval.py" \
  --baseline-only
```

## 产物位置

| 文件 | 内容 |
|---|---|
| `trace/.../ahe-component-report.json` | baseline、每轮候选、keep/revert、全部指标 |
| `trace/.../trace.jsonl` | 逐事件轨迹 |
| `workspace/.../round-N-<hash>/` | 被接受候选的完整快照 |
| `changes/round-*.json` | 每轮结构化 patch 提案与应用结果 |

被拒绝的候选自动清理。模板默认 `baseline_frozen: true`（候选永不覆盖源
目录）；如需直接写回源目录，可删除该键且不使用 `--no-write-back`，写回前
会自动备份为 `*.ahe-backup`。

## 导出最终版本

```bash
python3 plugins/ahe/ahe.py --export-best <trace目录>/ahe-component-report.json \
  --target /path/to/final-harness
```

导出命令将输出 baseline 与 final 的指标对比及使用方式（将原命令中的
harness 路径替换为导出目录即可）。导出副本默认自动注册：**Pi 侧**写入
packages 并按 `<harness>-<任务名>` 规则为注册命令改名（任务名默认取自评测任务，重启 Pi 后如
`/<harness>-<任务名>` 可用）；
**Codex 侧**若 harness 携带 `assets/codex/prompts/*.md` 模板或
`.codex-plugin/plugin.json`，则自动安装为带 `-v2` 后缀的 Codex 插件
（`commands/*.md` + marketplace manifest，经官方 `codex plugin` CLI
注册；可经 `$提及`/MCP 使用，命令模板的斜杠呈现依赖 codex 版本）。`--as-suffix` / `--no-register` /
`--no-rename` 同时调整两侧行为。

## 参考文档

- 完整模式说明与 keep 门禁：[README.md](README.md)
- 运行历史收敛分析：`skills/autoharness-convergence/SKILL.md`
