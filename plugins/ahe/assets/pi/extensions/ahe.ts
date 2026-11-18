// ahe.ts — Pi extension for AHE harness optimization
// Usage: /ahe 优化harness, harness路径为/path/to/harness, 提示词为审计代码, 指标为token降30%时间降20%准确率不变

import { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import * as path from "path";
import { fileURLToPath } from "url";

const PLUGIN_ROOT = path.resolve(dirname(fileURLToPath(import.meta.url)), "../../..");
const AHE_SCRIPT = path.join(PLUGIN_ROOT, "ahe.py");

interface AheArgs {
    harnessPath?: string;
    harnessRef?: string;
    manifestPath?: string;
    adapter?: string;
    evalCommand?: string;
    holdoutEvalCommand?: string;
    taskRoot?: string;
    evalTaskRoot?: string;
    evalTaskProject?: string;
    generateEvaluator?: boolean;
    outputManifest?: string;
    discoverAdapter?: boolean;
    task?: string;
    tokenTarget?: string;
    timeTarget?: string;
    accuracyConstraint?: string;
    rounds?: number;
}

function cleanValue(value: string): string {
    return value.trim().replace(/^['"`]|['"`,;。]$/g, "");
}

function parseArgs(raw: string): AheArgs {
    const result: AheArgs = {};
    const lines = raw.split(/[,，\n]/).map(s => s.trim()).filter(Boolean);
    for (const line of lines) {
        const harnessMatch = line.match(/^(?:harness(?:\s*(?:路径|path))?|路径)(?:为|=|:)?\s*(.+)$/i);
        if (harnessMatch) result.harnessPath = cleanValue(harnessMatch[1]);

        const manifestMatch = line.match(/^(?:manifest|harness\.manifest|清单)(?:\s*(?:路径|path|文件|file))?(?:为|=|:)?\s*(.+)$/i);
        if (manifestMatch) result.manifestPath = cleanValue(manifestMatch[1]);

        const adapterMatch = line.match(/^(?:adapter|适配器)(?:\s*(?:路径|path|名称|name))?(?:为|=|:)?\s*(.+)$/i);
        if (adapterMatch) result.adapter = cleanValue(adapterMatch[1]);

        const holdoutEvaluatorMatch = line.match(/^(?:--)?holdout(?:[-\s]?(?:eval(?:uator)?(?:-cmd)?|评测器|命令|command))(?:\s*(?:路径|path|命令|command))?(?:为|=|:)?\s*(.+)$/i);
        if (holdoutEvaluatorMatch) result.holdoutEvalCommand = cleanValue(holdoutEvaluatorMatch[1]);

        const taskRootMatch = line.match(/^(?:task[-\s]?root|任务(?:代码|工作负载|目标)?(?:路径|根目录))(?:为|=|:)?\s*(.+)$/i);
        if (taskRootMatch) result.taskRoot = cleanValue(taskRootMatch[1]);

        const evalTaskMatch = line.match(/^eval任务(?:路径|根目录|根)?(?:为|=|:)?\s*(.+)$/i);
        if (evalTaskMatch) {
            const evalRaw = cleanValue(evalTaskMatch[1]);
            const inner = evalRaw.match(/^(.*?)\s*里面的(?:的)?\s*(.+)$/);
            if (inner) {
                result.evalTaskRoot = cleanValue(inner[1]).replace(/[\/\s]+$/, "");
                result.evalTaskProject = cleanValue(inner[2]);
            } else {
                result.evalTaskRoot = evalRaw.replace(/[\/\s]+$/, "");
            }
        }

        const outputManifestMatch = line.match(/^(?:output[-\s]?manifest|输出(?:adapter|适配器)?manifest)(?:路径)?(?:为|=|:)?\s*(.+)$/i);
        if (outputManifestMatch) result.outputManifest = cleanValue(outputManifestMatch[1]);

        if (/(?:自动(?:生成|发现)\s*(?:adapter|适配器|修改面)?|discover[-\s]?adapter)/i.test(line)) {
            result.discoverAdapter = true;
        }

        if (/(?:自动)?(?:先生成|先写)?(?:生成|写)(?:一个)?(?:评估器|评测器)|generate[-\s]?evaluator/i.test(line)) {
            result.generateEvaluator = true;
        }

        const evaluatorMatch = /^eval任务/i.test(line)
            ? null
            : line.match(/^(?:--)?(?:真实\s*)?(?:eval(?:uator)?(?:-cmd|命令)?|评测(?:器|命令)?)(?:\s*(?:路径|path|命令|command))?(?:为|=|:)?\s*(.+)$/i);
        if (evaluatorMatch) result.evalCommand = cleanValue(evaluatorMatch[1]);

        const taskMatch = /^eval任务/i.test(line) ? null : line.match(/(?:提示词|任务)为?(.+)/);
        if (taskMatch) result.task = cleanValue(taskMatch[1]);
        const tokenMatch = line.match(/token降(?:低)?(\d+%?)/);
        if (tokenMatch) result.tokenTarget = tokenMatch[1];
        const timeMatch = line.match(/(?:时间|e2e)降(?:低)?(\d+%?)/);
        if (timeMatch) result.timeTarget = timeMatch[1];
        const accMatch = line.match(/准确率(.*)/);
        if (accMatch) result.accuracyConstraint = cleanValue(accMatch[1] || "不变");
        const roundMatch = line.match(/(\d+)轮/);
        if (roundMatch) result.rounds = parseInt(roundMatch[1]);
    }
    // Free-form fallbacks: /ahe <harness 路径或命令>, <任务描述>, <目标描述>, <指标>
    // The harness segment may be a bare path or a command reference like /my-tool.
    if (!result.harnessPath && !result.harnessRef && !result.manifestPath) {
        for (const line of lines) {
            if (/^\/[A-Za-z][\w-]*$/.test(line)) {
                result.harnessRef = line.slice(1);
                break;
            }
            if (/^(?:\/|\.{1,2}\/).+\/./.test(line)) {
                result.harnessPath = cleanValue(line);
                break;
            }
        }
    }
    if (!result.task && !result.evalTaskRoot) {
        const freeText: string[] = [];
        for (const line of lines) {
            if (result.harnessPath && line.includes(result.harnessPath)) continue;
            if (result.harnessRef && line === `/${result.harnessRef}`) continue;
            if (/^(?:优化)?harness|指标|降|不变|%|\d+\s*轮?\s*$|manifest|评测器|adapter|holdout|任务根目录|生成评估器|自动发现|路径为|任务为|提示词|eval任务|目标描述/i.test(line)) continue;
            if (line.length < 2) continue;
            freeText.push(cleanValue(line));
        }
        if (freeText.length) result.task = freeText.join("，");
    }
    if (!result.manifestPath && result.harnessPath && /(?:^|[\\/])(?:harness\.)?manifest\.json$/i.test(result.harnessPath)) {
        result.manifestPath = result.harnessPath;
        result.harnessPath = path.dirname(result.manifestPath);
    } else if (!result.harnessPath && result.manifestPath) {
        result.harnessPath = path.dirname(result.manifestPath);
    }
    return result;
}

function shellQuote(value: string): string {
    return `'${value.replace(/'/g, "'\\''")}'`;
}

function buildEngineFlags(args: AheArgs, generatedEvalCmd?: string): string {
    const flags = [
        "--task", shellQuote(args.task || "answer the task"),
        "--rounds", String(args.rounds || 3),
    ];
    if (args.tokenTarget) flags.push("--token-target", shellQuote(args.tokenTarget));
    if (args.timeTarget) flags.push("--time-target", shellQuote(args.timeTarget));
    if (args.accuracyConstraint) {
        flags.push("--accuracy-constraint", shellQuote(args.accuracyConstraint));
    }
    const evalCmd = args.evalCommand ? shellQuote(args.evalCommand) : generatedEvalCmd;
    if (evalCmd) flags.push("--eval-cmd", evalCmd);
    if (args.holdoutEvalCommand) {
        flags.push("--holdout-eval-cmd", shellQuote(args.holdoutEvalCommand));
    }
    if (args.taskRoot) flags.push("--task-root", shellQuote(args.taskRoot));
    if (args.evalTaskRoot) {
        flags.push("--eval-task", shellQuote(args.evalTaskRoot));
        if (!args.taskRoot) flags.push("--task-root", shellQuote(args.evalTaskRoot));
    }
    if (args.evalTaskProject) flags.push("--eval-project", shellQuote(args.evalTaskProject));
    if (args.discoverAdapter) flags.push("--discover-adapter");
    if (args.outputManifest) flags.push("--output-manifest", shellQuote(args.outputManifest));
    return flags.join(" ");
}

function needGeneratedEvaluator(args: AheArgs): boolean {
    return Boolean(args.generateEvaluator && !args.evalCommand);
}

function buildGenerateEvaluatorCommand(args: AheArgs, manifestExpr: string): string {
    const flags = [
        "--task", shellQuote(args.task || "answer the task"),
    ];
    if (args.evalTaskRoot) flags.push("--eval-task", shellQuote(args.evalTaskRoot));
    if (args.evalTaskProject) flags.push("--eval-project", shellQuote(args.evalTaskProject));
    flags.push("--manifest", manifestExpr);
    flags.push("--generate-evaluator");
    return [
        "python3", shellQuote(AHE_SCRIPT), shellQuote(args.harnessPath || "<harness路径>"),
        flags.join(" "),
    ].join(" ");
}

function buildManifestCommand(args: AheArgs, manifestPath: string): string {
    const generated = needGeneratedEvaluator(args)
        ? `python3 ${shellQuote(path.dirname(manifestPath) + "/ahe_evaluator.py")}`
        : undefined;
    return [
        "python3", shellQuote(AHE_SCRIPT), shellQuote(args.harnessPath || "."),
        "--manifest", shellQuote(manifestPath),
        "--adapter", shellQuote(args.adapter || "auto"),
        buildEngineFlags(args, generated),
    ].filter(Boolean).join(" ");
}

function buildAutoCommand(args: AheArgs): string {
    const harness = shellQuote(args.harnessPath || "<harness路径>");
    const adapter = shellQuote(args.adapter || "auto");
    if (args.discoverAdapter) {
        const flags = buildEngineFlags(args);
        return [
            `HARNESS=${harness}`,
            `python3 ${shellQuote(AHE_SCRIPT)} "$HARNESS" --adapter ${adapter} ${flags}`,
        ].join("\n   ");
    }
    const generatedEvalCmd = needGeneratedEvaluator(args)
        ? `python3 "$(dirname "$MANIFEST")/ahe_evaluator.py"`
        : undefined;
    const flags = buildEngineFlags(args, generatedEvalCmd);
    const genStep = needGeneratedEvaluator(args)
        ? [
            `  ${buildGenerateEvaluatorCommand(args, '"$MANIFEST"')}`,
            `  # 审阅生成的评估器（真实测量、非 stub、AHE-OBJECTIVE 已写入 manifest）后继续:`,
        ]
        : [];
    return [
        `HARNESS=${harness}`,
        `MANIFEST=""`,
        `for candidate in "$HARNESS/harness.manifest.json" "$HARNESS/manifest.json"; do`,
        `  if [ -f "$candidate" ]; then MANIFEST="$candidate"; break; fi`,
        `done`,
        `if [ -n "$MANIFEST" ]; then`,
        ...genStep,
        `  python3 ${shellQuote(AHE_SCRIPT)} "$HARNESS" --manifest "$MANIFEST" --adapter ${adapter} ${flags}`,
        `else`,
        `  python3 ${shellQuote(AHE_SCRIPT)} "$HARNESS" --baseline-only ${flags}`,
        `fi`,
    ].join("\n   ");
}

function buildGoalPrompt(args: AheArgs): string {
    const manifestMode = Boolean(args.manifestPath);
    const parts: string[] = [
        "## AHE Harness 优化任务",
        "",
    ];
    if (args.harnessPath) parts.push(`Harness 路径: ${args.harnessPath}`);
    if (args.harnessRef) {
        parts.push(`Harness 命令引用: /${args.harnessRef}`);
        parts.push(`先定位提供 /${args.harnessRef} 命令的插件/扩展源码目录（查 Pi settings 的 packages、已注册 extension 的源码位置），以该目录作为 harness 根继续`);
    }
    if (args.taskRoot) parts.push(`任务根目录（只读）: ${args.taskRoot}`);
    if (args.evalTaskRoot) {
        parts.push(`Eval 任务: ${args.evalTaskRoot}${args.evalTaskProject ? `（项目: ${args.evalTaskProject}）` : ""}`);
    }
    if (args.manifestPath) parts.push(`Harness manifest: ${args.manifestPath}`);
    if (args.adapter) parts.push(`Harness adapter: ${args.adapter}`);
    if (args.evalCommand) parts.push(`真实 evaluator: ${args.evalCommand}`);
    if (args.holdoutEvalCommand) parts.push(`真实 holdout evaluator: ${args.holdoutEvalCommand}`);
    if (args.task) parts.push(`任务描述: ${args.task}`);
    else if (args.evalTaskRoot) parts.push(`任务描述: 优化 harness 在 ${args.evalTaskProject || args.evalTaskRoot} 评测任务上的真实表现`);
    if (args.tokenTarget) parts.push(`Token 目标: 降低 ${args.tokenTarget}`);
    if (args.timeTarget) parts.push(`时间目标: 降低 ${args.timeTarget}`);
    if (args.accuracyConstraint) parts.push(`准确率约束: ${args.accuracyConstraint}`);
    parts.push(`优化轮数: ${args.rounds || 3}`);
    if (args.evalTaskRoot) {
        parts.push("");
        parts.push("## Eval 任务接线");
        parts.push("");
        parts.push(`- Eval 任务根目录: ${args.evalTaskRoot}（只读 benchmark checkout，绝不进入可变组件）`);
        if (args.evalTaskProject) {
            parts.push(`- 目标项目: ${args.evalTaskProject}（如位于 projects/ 下，直接使用目录名过滤）`);
        }
        parts.push("- 阅读该 benchmark 的 README、runners/、scripts/，选择其真实 runner/scorer 作为 evaluator，通过 --eval-cmd 传入 AHE；禁止自造简化评分");
        parts.push("- evaluator 收到环境变量 AHE_HARNESS_ROOT（候选 harness 目录）、AHE_EVAL_TASK_ROOT（任务根目录）、AHE_EVAL_TASK_PROJECT（目标项目）；评测对象是候选 harness，任务代码与 golden 数据只读");
        parts.push("- 若 benchmark 提供独立 holdout/验证集，通过 --holdout-eval-cmd 传入");
    }
    parts.push("");
    parts.push("## 执行步骤");
    parts.push("");
    parts.push("0. 运行前预检（只读，去源目录确认安装是否完成、如何运行;不要做多轮冒烟）:");
    parts.push("   ```bash");
    parts.push(`   python3 ${shellQuote(AHE_SCRIPT)} ${shellQuote(args.harnessPath || "<harness路径>")}${args.manifestPath ? ` --manifest ${shellQuote(args.manifestPath)}` : ""} --preflight`);
    parts.push("   ```");
    parts.push("   预检会报告安装入口、依赖状态（node_modules/.venv/运行探针）和运行方式；未就绪就先从源目录安装（把 --preflight 换成 --install-harness，安装入口优先级: manifest installation.command > install.sh > install.py > package.json > pyproject.toml）；然后直接 --baseline-only 真实验证——baseline 本身就是唯一的冒烟，不要在它之前再跑任何扩展/链路冒烟");
    if (manifestMode) {
        parts.push("1. 先读取 manifest，按其中声明识别可编辑组件。修改范围对齐 AHE 论文（arXiv:2604.25850，NexAU 框架）的 7 类可编辑组件：system prompt、tool description、tool implementation（运行时）、middleware、skill、sub-agent 配置、long-term memory（ledger/handoff）；论文消融显示收益主要集中在 tool implementation、middleware 和 long-term memory，优先这些组件");
        parts.push("2. 按 manifest 指定的 adapter 装载并运行真实 harness；从 evaluation.command/evaluation_command（或显式 evaluator 参数）取得真实 evaluator，validation.command 只用于候选校验；adapter/evaluator/runner 源码是可执行组件，不要把它们拼成 system prompt");
        if (needGeneratedEvaluator(args)) {
            parts.push("3. 先根据任务生成评估器和 keep 主指标（评估器是唯一裁判，keep 判据自动跟随它声明的主指标 + 本命令的指标目标）：");
            parts.push("   ```bash");
            parts.push(`   ${buildGenerateEvaluatorCommand(args, shellQuote(args.manifestPath || "harness.manifest.json"))}`);
            parts.push("   ```");
            parts.push("   生成后必须审阅：真实运行 harness、输出含主指标的 JSON、非 stub、不修改任务目录；生成器已把 AHE-OBJECTIVE（metric+direction）写入 manifest，keep 门禁自动使用");
            parts.push("4. 通过 AHE 引擎执行完整的基线、进化和真实 evaluator 回归：");
            parts.push("   ```bash");
            parts.push(`   ${buildManifestCommand(args, args.manifestPath || "harness.manifest.json")}`);
            parts.push("   ```");
            parts.push("5. 只在 manifest 声明的组件边界内修改（7 类组件中对齐论文声明的部分），依据 evaluator 与 holdout evaluator 输出的 score/F1、tokens、time 决定保留或回退；源目录始终冻结，只保留 candidate");
            parts.push("6. manifest.references 只作为修改 Agent 的只读参考材料（例如公开机制模式），不能直接注入 harness runtime；evolve 允许并鼓励上网搜索相关 skill、中间件模式与相关论文（AHE 论文 arXiv:2604.25850、NexAU、公开 coding-agent harness 等）作为修改参考——参考只出思想，patch 锚点必须来自当前组件文件");
            parts.push("7. 汇报真实 evaluator/holdout evaluator 的优化前后结果和实际修改组件");
        } else {
            parts.push("3. 通过 AHE 引擎执行完整的基线、进化和真实 evaluator 回归：");
            parts.push("   ```bash");
            parts.push(`   ${buildManifestCommand(args, args.manifestPath || "harness.manifest.json")}`);
            parts.push("   ```");
            parts.push("4. 只在 manifest 声明的组件边界内修改（对齐 AHE 论文 7 类组件），依据 evaluator 与 holdout evaluator 输出的 score/F1、tokens、time 决定保留或回退；源目录始终冻结，只保留 candidate");
            parts.push("5. manifest.references 只作为修改 Agent 的只读参考材料（例如公开机制模式），不能直接注入 harness runtime；evolve 允许并鼓励上网搜索相关 skill、中间件模式与相关论文（AHE 论文 arXiv:2604.25850、NexAU、公开 coding-agent harness 等）作为修改参考——参考只出思想，patch 锚点必须来自当前组件文件");
            parts.push("6. 汇报真实 evaluator/holdout evaluator 的优化前后结果和实际修改组件");
        }
    } else {
        parts.push("1. 先检查 harness.manifest.json 或 manifest.json；如果存在，必须切换到 manifest/adapter 模式，不得把所有 .py/.ts 运行时代码当作 prompt");
        if (args.discoverAdapter) {
            parts.push("2. 先执行自动 adapter/修改面发现命令；它只生成草稿，不会修改 harness 或任务仓库；发现范围对齐 AHE 论文 7 类组件（system prompt、tool description、tool implementation、middleware、skill、sub-agent 配置、long-term memory）");
            parts.push("3. 审阅输出中的 selected、review、excluded 和 warnings：对照论文 7 类组件清单，确认 harness 的各类可编辑组件都已进入 mutable components，同时 task/evaluator/kernel/golden 均未进入");
        } else {
            parts.push("2. 用 bash 执行下面的自动分支命令：");
        }
        parts.push("   ```bash");
        parts.push(`   ${buildAutoCommand(args)}`);
        parts.push("   ```");
        if (args.discoverAdapter) {
            parts.push("4. 草稿通过审阅后，再用生成的 manifest 重新运行 AHE 优化；不要把 discovery 输出直接当作已批准的修改范围");
        } else {
            parts.push("3. 如果命令找到 manifest：bash 块中的评估器生成命令会先根据任务写评估器并更新 keep 主指标（AHE-OBJECTIVE 写入 manifest），审阅生成结果后再让优化循环继续；不要跳过审阅直接放行");
            parts.push("4. 未发现 manifest 时，**禁止直接手改 harness 源码做优化循环**；必须先用 discovery 生成 manifest 再走组件模式:");
            parts.push("   ```bash");
            parts.push(`   python3 ${shellQuote(AHE_SCRIPT)} ${shellQuote(args.harnessPath || "<harness路径>")} --discover-adapter --task ${shellQuote(args.task || "answer the task")}${args.evalTaskRoot ? ` --eval-task ${shellQuote(args.evalTaskRoot)}` : ""}${args.evalTaskProject ? ` --eval-project ${shellQuote(args.evalTaskProject)}` : ""}${args.evalCommand ? ` --eval-cmd ${shellQuote(args.evalCommand)}` : ""} --validation-cmd "python -m compileall ." --output-manifest ./harness.manifest.json`);
            parts.push("   ```");
            parts.push("   审阅 selected/review/excluded/warnings（确认任务/评测器/golden 未进可变面）后，用 `--manifest ./harness.manifest.json` 进入组件模式优化循环——隔离候选、锚点 patch、门禁、自动 keep/revert 都由引擎管，改坏自动回滚");
            parts.push("5. 旧的手动编辑循环（改文件→重测→不降才保留）仅当 harness 是单一 prompt 文件且用户明确要求手动模式时才允许");
            parts.push("6. 汇报最终结果：优化前后的 accuracy / tokens / time 对比");
        }
    }
    parts.push("");
    parts.push("## 收尾");
    parts.push("");
    parts.push("1. campaign 结束后引擎已自动在 <trace目录>/patch-report/ 生成 harness/patches 风格的报告（README.md + 每轮累计 .patch/.md）；如需指定目录（例如归档进仓库 harness/patches/<类别>/<campaign>/），用 AHE_PATCH_REPORT_DIR 环境变量或事后重导出:");
    parts.push("   ```bash");
    parts.push(`   python3 ${shellQuote(AHE_SCRIPT)} --export-patch-report <trace目录>/ahe-component-report.json --target <报告目录>`);
    parts.push("   ```");
    parts.push("2. 导出最终版 harness（默认自动按 <harness>-<任务名> 规则改名并注册为 Pi 插件，重启 Pi 后即可用新命令）:");
    parts.push("   ```bash");
    parts.push(`   python3 ${shellQuote(AHE_SCRIPT)} --export-best <trace目录>/ahe-component-report.json --target <导出目录>`);
    parts.push("   ```");
    parts.push("把 patch 报告位置、导出命令打印的指标对比、使用方式和新命令转达给用户");
    parts.push("");
    parts.push("## 约束");
    parts.push("- 禁止在 harness 中嵌入答案或匹配规则");
    parts.push("- 禁止删除导致准确率下降的内容");
        parts.push("- 禁止多轮冒烟：预检一次 + baseline-only 一次就是全部验证");
    parts.push("- 优化必须走 manifest/组件模式（隔离候选+门禁+自动回滚）；未发现 manifest 时先 --discover-adapter 生成，禁止直接编辑 harness 源码做优化（单一 prompt 文件且用户明确要求手动模式的除外）");
    if (args.evalTaskRoot) {
        parts.push("- eval 任务目录（含 projects、golden、runner、scorer）只读；不得修改任务代码或评分逻辑来凑指标");
    }
    if (manifestMode) {
        parts.push("- evaluator 必须是真实 manifest/adapter 配置的评测器；禁止用 API 可用性探测或字符串匹配冒充真实结果");
        if (args.holdoutEvalCommand) {
            parts.push("- holdout evaluator 是独立回归门槛，必须真实执行并与开发 evaluator 分开报告");
        }
        parts.push("- 若 manifest 没有 evaluation.command/evaluation_command，先定位真实 evaluator 并通过 `--eval-cmd` 传入；不得把 validation.command 当作 evaluator");
        parts.push("- 不得把 adapter、evaluator、runner 或工具实现源码作为 prompt 内容发送给模型");
    }
    if (args.accuracyConstraint?.includes("不变")) {
        parts.push("- 准确率绝对不能下降");
    }
    return parts.join("\n");
}

function dirname(p: string): string {
    return p.substring(0, p.lastIndexOf("/"));
}

export default async function (pi: ExtensionAPI) {
    pi.registerCommand("ahe", {
        description: "AHE harness 优化：eval任务为<benchmark路径>里面的<项目>, 或 manifest/adapter/真实 evaluator",
        handler: async (args: string, ctx: ExtensionContext) => {
            const raw = args.trim().replace(/^\/?ahe[\s,，]+/i, "");
            if (!raw) {
                ctx.ui.notify(
                    "用法: /ahe <harness 路径或命令（如 /my-tool，自动识别）>, " +
                    "<任务描述>, <目标/指标>, N轮；也可用显式段: " +
                    "eval任务为<benchmark>里面的<项目>, manifest路径为..., 评测器为..., 生成评估器",
                    "error",
                );
                return;
            }

            const parsed = parseArgs(raw);
            if (!parsed.harnessPath && !parsed.harnessRef) {
                ctx.ui.notify("错误: 未找到 harness（路径或命令）。可用 '/ahe /my-tool, 任务描述, token降30%, 3轮' 或 'harness路径为/xxx' 指定", "error");
                return;
            }

            // 构建优化任务 prompt 并触发 agent 执行
            const goalPrompt = buildGoalPrompt(parsed);
            pi.sendMessage(
                {
                    customType: "ahe-optimization",
                    content: goalPrompt,
                    display: false,
                    details: {
                        harnessPath: parsed.harnessPath,
                        harnessRef: parsed.harnessRef,
                        manifestPath: parsed.manifestPath,
                        adapter: parsed.adapter,
                        evalCommand: parsed.evalCommand,
                        holdoutEvalCommand: parsed.holdoutEvalCommand,
                        taskRoot: parsed.taskRoot,
                        evalTaskRoot: parsed.evalTaskRoot,
                        evalTaskProject: parsed.evalTaskProject,
                        generateEvaluator: parsed.generateEvaluator,
                        outputManifest: parsed.outputManifest,
                        discoverAdapter: parsed.discoverAdapter,
                    },
                },
                { triggerTurn: true, deliverAs: "followUp" },
            );
        },
    });
}
