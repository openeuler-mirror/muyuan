# 调试运行时状态

本文用于检查正在运行或已经结束的 Goal Plus（Codex 使用 `$goal-plus` ，Pi 使用 `/goal-plus` ）：main 和 worker 处于什么状态、Search
产生了哪些 verifier Evidence，以及故障发生在 runtime 还是 host。

## 推荐排查顺序

1. 先读取 `goal_plus_monitor_snapshot`，确定 goal revision、work item、Search run、candidate 和 warning；
2. monitor 缺少所需细节时，再检查 `.gp/` 中的持久状态和 verifier log；
3. 只有需要还原 worker 行为或 host lifecycle 时，才读取 Codex/Pi 原生日志；
4. 始终通过 `goal_plus_id`、`run_id`、`candidate_id` 和 `agent_session_id` 交叉关联，不能从 prose 猜测缺失事实。

`goal_plus_monitor_snapshot` 的 `execution_scope` 字段是首选生命周期诊断入口，包含 scope state、
active count、close reason 和 cleanup errors。仅当该投影不足时查看
`.gp/host-scopes/<scope_id>/scope.json`。Scope 是 host-local 活计算证据，不是 Search run 状态；
不要通过手改 scope 文件尝试 resume。

## 长时间观察

真实 ST/E2E 和长时间 Search 先复用以下只读脚本，无需临时编写监控器或 JSONL 解析器：

```bash
scripts/monitor_goal_plus.sh --once /path/to/project/.gp
scripts/monitor_goal_plus.sh --interval 10 /path/to/project/.gp
python scripts/check_session_instructions.py /path/to/pi-session.jsonl \
  --agent-harness pi --overview /path/to/session-build/assets/goal-plus-instructions.txt \
  --recent 6
```

Monitor 汇总 Goal、候选、Evidence、session 与用量；`--json` 提供机器可读快照。
Session audit 的 `--recent N` 自动启用工具分析，沿选定 Pi branch 显示最近 N 次 GP 调用、
provider 错误及模型标识，不输出完整 schema、参数或思考内容。N 为 1–100；需要正文时追加
`--include-content`，每条结果或错误最多显示 800 字符，截断会明确标注。
`outcome=returned` 只表示宿主未标记错误；若返回的结构化 `details.ok` 存在，另列
`reported_ok`，不要把工具返回等同于业务成功。尚无结果的调用保持 unavailable。

明确传入本次运行的 runtime/session 路径，不扫描全部用户会话。脚本只观察，不启动模型、
发送消息或修改运行状态；等待、停止和恢复仍使用正式宿主入口。正在追加的日志可能暂时
不完整，unavailable 时稍后重读，不据此宣称 worker 丢失。`--overview` 应来自该 session
实际运行的 build。更深入的 schema、成本和完整结果分析继续使用下文的 audit 选项。
需要逐行查看 Main/subagent 实际对话与注入内容时，使用
[Agent 行为全量导出](#agent-行为全量导出goal_plus_debug)。

## 任务结束后检查概要是否保留

这是诊断观测，不参与任务完成判定。所有额外的 session 观测、归档、大小统计和 HTML
导出统一由 `GOAL_PLUS_DEBUG=1` 开启：

```bash
GOAL_PLUS_DEBUG=1 pi
GOAL_PLUS_DEBUG=1 codex
```

未设置或不等于 `1` 时不新增这些诊断数据。宿主原生消息、工具调用、业务状态、
概要的正常加载和 Codex 压缩后注入不受影响。
宿主不会自动执行事后审计；下面的离线脚本由用户手动调用，无需再次设置环境变量。

使用 [session 检查器](../scripts/check_session_instructions.py) 读取原生历史，不调用模型或修改 session：

```bash
python scripts/check_session_instructions.py /path/to/rollout.jsonl \
  --agent-harness codex --overview /path/to/session-build/assets/goal-plus-instructions.txt
python scripts/check_session_instructions.py /path/to/pi-session.jsonl \
  --agent-harness pi --overview /path/to/session-build/assets/goal-plus-instructions.txt
```

`--overview` 必须指向该 session 所用版本的原文；不能用新版文案检查旧 build 后直接判丢失。
JSON 输出包含 `expected_sha256`、`compactions`、逐次 `checks` 的行号及结果：

| 结果 / 退出码 | 含义 |
| --- | --- |
| `present` / 0 | 所有已检查推理轮次均有该概要的直接证据 |
| `missing` / 1 | 至少一轮在可检查范围内缺少精确概要，或文字已变化 |
| `unavailable` / 2 | 历史损坏、版本不符、缺少观测或压缩后尚未推理，不能确认 |

Codex 检查 `replacement_history` 和之后的 system/developer 消息；每次压缩重置旧证据。
`guardian_history`、助手复述、工具返回和摘要中的偶然出现不算说明保留。其 `evidence_scope`
为 `recorded_working_context`：rollout 不包含完整 provider request，不能据此断言隐藏的
MCP 动态描述也缺失。记录匹配同样不证明模型遵循了流程。

有活动 Goal Plus 时，Pi 于 `turn_start` 读取宿主最终 system prompt，并将概要哈希、匹配布尔值及
session/turn 身份写入 `goal-plus-instructions-observation` 原生 custom entry。普通会话不注入
概要，也不写这类 observation。它不写完整 prompt，也不
进入模型上下文。检查按分支祖先链进行；默认检查最后持久 entry 的分支，也可通过
`--leaf-id` 指定分支。旧 Pi session 或关闭采集的会话缺少观测时只能返回 unavailable。

默认检查对象是共同流程概要，不评估摘要语义是否等价。
两端真实压缩测试保存 `instructions-audit.json`；此前 Codex 全 JSON 文本搜索会误匹配
`guardian_history`，该旧断言不能作为压缩后的说明保留证据。

### 分析工具描述与调用失败

开启 DEBUG 时，Pi 额外记录 `goal-plus-tool-contracts` 和
`goal-plus-provider-request` custom entries。前者保存 Goal Plus 注册时与 provider request
hook 中的工具名称、description、schema；相同快照仅在当前分支未保存时追加。
后者绑定 session、request、turn、模型和合同哈希，不保存请求正文或认证 header。
请求字节数仅在 `GOAL_PLUS_DEBUG=1` 时计算，默认不额外序列化完整请求。
观测位于该 hook 执行时点，不能证明后续 extension 未修改请求，也不证明模型理解了描述。
目前解码 OpenAI function/Responses 与 Anthropic tool 格式；其他格式报告 unavailable。

使用 session 对应的受管 Python（需要 `jsonschema`）运行：

```bash
python scripts/check_session_instructions.py /path/to/pi-session.jsonl \
  --agent-harness pi --overview /path/to/session-build/assets/goal-plus-instructions.txt \
  --tools > /path/to/session-tools-analysis.json
```

`tool_analysis` 列出原文合同、逐次请求、assistant 结束状态、调用及结果行号。
`turns` 保留没有工具调用的 provider 错误轮次，避免把 HTTP 限流等问题误判为工具错误；
`--include-content` 同时显示原生 provider 错误正文。`description_matches_registration`
和 `schema_matches_registration` 用于定位 bridge/provider 转换；`schema_issues` 标明
缺失字段、额外字段与嵌套路径，`previous_call_id` / `same_arguments` 用于检查报错后的
修正。`outcome=returned` 仅表示宿主未标记工具错误，不表示业务成功或 Goal 完成。
工具参数与返回正文默认仅输出哈希；需要阅读实际参数、返回值和错误正文时追加 `--include-content`，
输出仅保存在本地，不提交到仓库。分析不输出模型思考，不执行工具，不访问远程 schema。
`schema_status=valid` 只代表匹配该请求的公开参数 schema；完整 SearchSpec、归属和当前
业务状态仍由工具用例校验，其错误应结合 `error_text` 和持久状态阅读。

旧 session 可追加 `--tool-manifest /path/to/session-build-tools-list.json`，与同 build 的
`{"tools": [...]}` MCP manifest 比较；该结果标为 `reference_only`，不作为请求可见性证据。
缺少或损坏观测时 `tool_analysis.status=unavailable`，启用 `--tools` 的命令退出码为 2。
Codex rollout 没有相应 provider request 观测时同样保持 unavailable。

定位时先检查合同是否送达和是否被转换，再查看失败调用、原始错误、下一次参数变化。
schema 合法但持续无法推进时，还需结合 Goal 状态和实际工具结果判断描述是否解释清楚了
任务及前置条件；不能仅凭重复失败就归因为模型能力或网络故障。

### 自动保留与增量归档

普通 Pi 的原生历史默认位于 `~/.pi/agent/sessions/<工作目录编码>/`。开启 DEBUG 后，GP 每次执行结束将该
session 的历史路径和 build 概要路径写入 `<runtime>/host-logs/session-diagnostics/pi-<hash>/index.json`，
不复制原生历史。TT Root 的 runtime 位于其受管 session 目录，不能假定是目标工作区的 `.gp/`。

开启 DEBUG 时，TT Candidate/Annotation Child 每次执行结束通过原有父子 Message 通道发送新增 native
session entries。Root 校验已绑定的 Child 和 agent session，将批次保存到
`<runtime>/host-logs/session-diagnostics/<child-id>/`，删除 Child 前再补收尾部消息。
同一条归档流同时服务运行诊断和离线行为复盘；批次包含压缩校验、原生 session、stream、
顺序号和 entry identity。重放不重复保存，成功发送后的游标保存在原生
`goal-plus-session-archive-checkpoint` custom entry 中，同一 Child 的 Pi 进程重建后继续
发送新增条目，不重新发送整个历史；游标不进入模型上下文。清理使用独立 Message cursor，
不执行或重放业务工具。每个目录的 `index.json` 关联 Root/run/candidate/session，并记录
批次条目数、压缩大小、解码大小和清理时收取状态。

归档保留完整 Pi native session entry，内容可能包含 prompt、thinking、工具参数和返回正文，
只供本地 host 侧诊断和行为复盘，不提交到仓库。每批压缩上限为 1 MiB、解码上限为 16 MiB；
完整 session 会按大小切成多个批次。单个 entry 超过边界或发送失败时，保留原生记录并报告
`goal_plus_session_archive_unavailable`，不阻止推理和业务清理。
发送复用官方 SDK，由限时 5 秒的独立 helper 执行；超时回收 helper，后续执行结束可重试同批。
突然退出前尚未发送的尾部、外部删除整个 Root 后的 runtime 不在保留保证内。
`collection_complete` 只表示当时已读完可用 Message 页，不证明 Child 所有尾部均已发送。

分析器也接受上述单个 session 目录，可直接分析清理后保留的 Child 批次：

```bash
python scripts/check_session_instructions.py /path/to/runtime/host-logs/session-diagnostics/tt-child-id \
  --agent-harness pi --overview /path/to/session-build/assets/goal-plus-instructions.txt \
  --tools > child-session-analysis.json
```

目录内损坏记录、缺失批次或不完整祖先链返回 unavailable。Child 没有 Main 概要观测时，
顶层概要检查仍可为 unavailable，应单独查看 `tool_analysis.status`。分析默认隐藏 GP 参数和
结果正文；`--include-content` 才在报告中展开。索引和采集都不自动运行分析器或调用模型。

### 注入台账

扩展向模型上下文注入 Goal Plus 内容的每个位置（per-turn goal context 的 full/brief/skipped
三分支、must-continue、publication 通知、start/edit/native continuation、worker continuation、
TT missing-attempt、worker steering、stale 清理）都会先写一条
`goal-plus-injection-observation` custom entry：kind、role、channel、目标 id、字符数、
sha256 和启发式 token 估算（CJK 约 1 token/字符、ASCII 约 4 字符/token；注入正文本身不落盘）。
条目随上述归档链路保留，注入是每 turn 一次性事件，无每推理成本。

`check_session_instructions.py` 默认汇总 `injection_ledger`：按 role×kind 统计次数与估算
token，并把每条注入关联到之前最近一次 provider 输入用量（即注入时上下文规模，例如
"main 在 ~100k 上下文时注入一次 goal_context_full，估算 +407 tokens"）。三条健康规则
会把报告降级为 missing：

- `full_context_without_raw_goal`：goal_context_full 触发但载荷没有 raw_goal（节流触发、
  补货失效，如 status 刷新未带 detail）；
- `goal_context_skipped_on_prompt_turn`：prompt 路径 turn 被标为 continuation 而跳过注入。
  must-continue 抑制标志已删除（followUp turn 由宿主路由天然绕过 before_agent_start），
  该规则保留为防回归 tripwire；
- `registered_injection_absent`：登记了 custom message 注入但分支上找不到对应消息。

新增任何模型上下文注入必须先过 `appendInjectionObservation`
（`assets/pi/extensions/lib/goal-plus-session-diagnostics.ts`），否则上述看护对该注入失效。

## 上下文占用检查

现有 session 检查器支持 `--context`，离线统计所选 Pi 分支中实际观察到的 GP 工具
description/schema、调用参数、返回正文及字段占比，并列出宿主记录的每轮输入 token
（含 cacheRead/cacheWrite）。无需重新执行任务或开启 debug；缺少观测时报告 unavailable。

```bash
PYTHONPATH=src python scripts/check_session_instructions.py /path/to/session.jsonl \
  --agent-harness pi --overview /path/to/session-build/assets/goal-plus-instructions.txt \
  --context --token-encoding o200k_base --checkout-tools main \
  --output /tmp/goal-plus-context.json
```

`--token-encoding` 需要环境中安装 `tiktoken`；不指定时仍统计精确 UTF-8 字节。
token 数仅为指定编码对记录文本的估算，不等于 provider 的工具包装、当前上下文或计费。
`--checkout-tools main|candidate|final_checker` 在临时目录枚举当前源码的角色 schema，
不启动宿主、不调用模型或工具；结果标为 reference_only，同时提供与历史相同工具集合的比较。
运行此选项需要当前源码依赖。也可用 `--tool-manifest` 指定已有 tools/list JSON。

报告按合同快照列出启动时的工具定义开销，按工具列出累计返回量和最大单次返回；
累计返回量不是当前上下文占用，compaction/宿主裁剪可能已经移除旧正文。
概要大小来自指定 build 的参考文本，其送达证据仍由原检查负责。完整 system prompt、
原生工具、图片以及压缩后的保留内容不从日志推测；Codex 缺少 provider 工具观测时保持
unavailable。默认报告不包含参数/返回正文或模型思考，`details` UI 副本不会计入正文两次。
TT Child 的诊断归档目录同样可作为输入；报告只保存在本地，不提交到仓库。

## Agent 行为全量导出（GOAL_PLUS_DEBUG）

session-diagnostics 归档现在保留完整 session entry，注入台账仍只保存哈希。需要逐行检查
Main/subagent 实际做了什么（长时间无输出、临时注入的
prompt、工具调用与返回正文）时，使用本导出。它在导出时回读本地原生日志，内容完整，
属于显式 debug 通道，输出两种形式：

- `index.html`：自包含静态页面（无脚本），会话导航、类型配色、注入高亮、`⏸ gap`
  停顿标记、可折叠正文与内容足迹统计，是主要的人读视图；
- `events.jsonl`：每行一个事件的完整 JSON，供离线脚本消费，也可 `tail -F` 观察增量。

HTML 中每个事件正文默认展开（`meta`/`context` 观测记录折叠），会话标题与导航用
`candidate c001`、`main` 这类标识区分各 lane，进入会话的真实 prompt 前有
`▶ prompt #N · dispatch/continuation` 分界线。每个会话还给出内容足迹统计：按 kind+label
分组的字符数与启发式 token 估算（CJK 约 1 token/字符、ASCII 约 4 字符/token，非计费口径），
并区分进入模型上下文的事件与仅占磁盘的宿主记录，用于定位可删减的冗余。
生成 `goal_plus_search_report` 时若本地已存在该导出，report.html 末尾会附上指向它的链接。

手动导出整个 runtime root，自动发现并合并 Pi Main 原生历史、Search candidate、TT Child
诊断归档、controller journal、Codex rollout 和 time advisory：

```bash
PYTHONPATH=src python scripts/export_agent_transcript.py /path/to/project/.gp
PYTHONPATH=src python scripts/export_agent_transcript.py --watch --interval 5 /path/to/project/.gp
tail -F /path/to/project/.gp/host-logs/agent-transcript/events.jsonl
```

输出默认写入 `<root>/host-logs/agent-transcript/`（每个会话一个子目录，内含
`transcript.html`），`--watch` 在源文件变化时持续刷新，适合观察长时间运行的任务。也可以直读单个原始文件：

```bash
PYTHONPATH=src python scripts/export_agent_transcript.py --pi /path/to/pi-session.jsonl --role main
PYTHONPATH=src python scripts/export_agent_transcript.py --codex /path/to/rollout.jsonl
```

常用选项：`--leaf-id`（Pi 分支，默认最后持久 entry）、`--max-chars`（HTML 正文
截断上限）、`--gap-seconds`（停顿标记阈值，0 关闭）、`--output`、`--no-jsonl`。

正常启动只需 `GOAL_PLUS_DEBUG=1`，不需要启动包装器或单独的后台 watcher：

```bash
GOAL_PLUS_DEBUG=1 pi
GOAL_PLUS_DEBUG=1 codex
GOAL_PLUS_DEBUG=1 tt run --profile goal-plus-pi --fs <workspace> -- --model <provider/model>
```

- Codex Stop/SubagentStop 保存当前原生历史的诊断索引，并刷新整个 runtime 的合并页面。
- Pi 在 agent_end 保存原生历史索引并调用受管 Python 导出；Main 在 session_shutdown
  关闭 MCP 后再刷新一次，子进程退出不重复启动导出器；
  direct child 继承 Main 的 DEBUG 开关。
- TT Child 经已有 Message 通道发送增量 session entry；Root 在完整批次归档后刷新页面，
  释放前补收并导出。Root 无需访问兄弟 Child 目录，已收到的归档可用于释放后导出。
- 报告生成前刷新导出，`report.html` 可链接完整 `index.html`。
- 合并导出按 runtime 加锁，保留所有已发现会话；失败只记录诊断，不改变业务结果。

自动刷新发生在上述事件边界，并非逐 token 实时刷新。前面的离线导出命令仍可用于
历史数据或自定义输出目录；`--watch` 仅是可选的手动观察方式，不是正常启动依赖。

边界与限制：

- 输出包含 prompt、工具参数、返回正文和模型思考，只保存在本地 host-logs，不要提交。
- TT Child 视图来自 session-diagnostics 归档，包含完整归档期间已收到的 session entry；
  如果 Child 在 flush 前突然退出，尾部仍可能显示为 partial。
- Pi Main 依赖原生历史仍存在（`session-diagnostics/pi-*/index.json` 的 history 指针）；
  Codex reasoning 摘要取决于 rollout 是否记录，缺失时不推测。
- 导出器只读：不启动模型、不发送消息、不修改运行状态；与 monitor/audit 脚本可同时使用。

## API 返回大小 Debug

在启动宿主前设置 `GOAL_PLUS_DEBUG=1`，逐次输出接口返回大小：

```bash
GOAL_PLUS_DEBUG=1 codex
GOAL_PLUS_DEBUG=1 pi 2>goal-plus-api-debug.log
GOAL_PLUS_DEBUG=1 goal-plus-pi-tool goal_plus_status \
  --root .gp --args-json '{"goal_plus_id":"gp_..."}'
```

Codex 的 MCP server 进程也需要继承该环境变量；已启动的 server 需重启。统计写到各自
进程的 stderr，MCP stderr 是否直接显示由 Codex 决定。Pi 扩展在最终工具返回处直接写入
宿主 stderr；Python CLI 的 stderr 被 Pi 捕获时仍可通过 `pi-tool` 记录查看最终响应。
共同 session RPC 会持续排空 stderr；是否保存诊断由宿主 callback 决定，不保证生成独立
`pi-rpc-*.jsonl` 文件。旧 worker 日志路径不适用于新的 session actor。
不要把本地 debug 日志提交到仓库。

示例统计行（字节数仅作示意）：

```json
{"event":"goal_plus_api_response_size","transport":"mcp","tool":"goal_plus_record_search_result","format":"json","bytes":430,"characters":378}
```

- `mcp`：全部公开工具的返回值。JSON 按紧凑 UTF-8 序列化计数；字符串和
  `TextContent` 按原文本计数，不包含 MCP envelope 或额外的 structured-content 副本。
- `pi-cli`：全部 CLI 工具与 host-command 成功返回的实际 JSON 字节数，包含 `--pretty`
  格式化，不包含 stdout 的最后一个换行。
- `pi-tool`：Pi 注册的 Goal Plus 工具最终结果按 JSON 序列化的字节数，覆盖 Pi 专有工具
  和 ThinkThread 工具。另列 `text_bytes`（所有 text content 的 UTF-8 字节数）及
  `details_bytes`（details 的 JSON 字节数）。这两项与整体 `bytes` 不是可相加的独立开销。
- `characters`：同一序列化文本的 Unicode 码点数，含字段名、标点和路径；Pi 另列
  `text_characters`、`details_characters`。它不是中文汉字数或 tokenizer 结果。

这些是不同返回边界的大小，不是 token 数或模型计费，也不能跨 `transport` 累加为模型
输入量。统计只含工具名、格式与大小，不打印参数或返回正文，不修改 API payload 或领域
状态。默认关闭；开启时额外序列化可能增加少量开销。抛出的异常没有返回值，因此不计数；
以正常结果返回的错误对象仍计数。

### ThinkThread Pi

先通过 `./install.sh --pi --thinkthread` 刷新 `goal-plus-pi` Profile，再启动新的 Root：

```bash
GOAL_PLUS_DEBUG=1 tt run --profile goal-plus-pi --fs /path/to/workspace
```

当前受支持的 TT Pi 基线会将启动环境中的开关传给 Root 和 Child；两者的 Goal Plus 工具
共用 `pi-tool` 统计包装器，Child RPC 返回也经过它。统计写到各自 Pi 的 stderr，再由 TT
接入各自的终端流。需要保存时应从运行开始记录终端输出；对已退出的实例再执行
`tt attach` 不能作为历史日志读取方式。

Main 的终端记录不等于 Child 日志汇总，`.gp/host-logs/pi-rpc-*` 只适用于 Pi RPC worker。
基础 session 诊断按上节自动索引和归档；stderr 大小统计仍没有自动聚合采集器。开关传递和 Root 统计输出已做真实探针
验证，不代表每条 TT Search/Annotation 工具链都已完成真实验证。

## Monitor Snapshot

`goal_plus_monitor_snapshot` 是首选的只读入口。它不会启动、等待、继续或停止 worker。MCP 不便直接调用时，可以使用 Pi facade：

```bash
goal-plus-pi-tool goal_plus_monitor_snapshot \
  --root .gp \
  --args-json '{"goal_plus_id":"gp_...","stale_after_seconds":600,"detail":true}' \
  --pretty
```

模型轮询默认使用 `detail=false`；本节以下列出的配置、详细统计和 session 字段需要 `detail=true`。
Goal 的完整目标/草案用 `goal_plus_status(detail=true)`，事件和历史记录用
`goal_plus_history` 分页读取，候选调用观测用 `goal_plus_search_get_agent_observability`。
裁剪只改变工具返回；持久审计文件、Monitor/dashboard 和报告数据保持完整。

只知道 `run_id` 时也可以直接查询该 run。需要关闭可选投影时传入 `feature_plugins=[]`。
可选视图的使用见 [Feature Plugins](feature-plugins.md)，返回字段见 [API](api.md#handoff-与-observability)。

### Goal 与 Subagent 派发

首先检查：

- `goal_revision`、`goal_revisions_total`、`phase` 和 `next_action`；
- `feature_plugins.orchestration.dispatches` 中返回的结果是否已有 Main 的 `accepted`、`rework` 或 `cancelled` 决定；
- subagent attempt 的 `attempt_id`、`generation`、launch deadline 和 bound handle；
- `feature_plugins.orchestration` 中是否存在迟到的 `stale_result`，以及 main 是否已经记录 acceptance/rework decision；
- `final_check_policy` 和 `latest_final_check` 是否对应当前 revision。

`dispatch` 后长期停留在 `launching` 通常表示 host 尚未成功 `bind` 。新 generation 已经派发后，旧 generation 的 result 只会成为
`stale_result` Evidence，不会推进当前 work item。

### Search Run

一条 goal 可以追加多个 Search task。`search_tasks[]` 是完整历史，`linked_search` 和顶层详细 `run` 只是当前 task 的兼容视图。不要用
`linked_search != null` 推断 task 数量。

重点字段包括：

- run state、frozen spec、strategy 和唯一初始 plan；
- candidate score、iteration、session、verifier count 和 current best；
- worker duration、usage、context、artifact 和 stale/timed-out warning；
- selection、promotion 和 report path；
- ThinkThread 的 `host_cleanup.state` 以及 candidate sessions、annotation、snapshot 和 final artifact
  四项摘要；
- `statistics.selected_run` 中的 baseline/target improvement、
  time-to-first-verifier/success、worker outcome、model/provider、selection survival、usage、
  efficiency、workspace 创建/apply 耗时和 `unavailable_metrics`；
  `workspace_operations` 保留 provider、观测次数、成功/失败与耗时聚合，`timing`
  保留报告使用的扁平秒数字段。

`search_task_aggregate` 汇总全部 task 的 plan、candidate、session、verifier 和已知
cost。`statistics.orchestrator` 只包含归一化 Codex usage 与 identity metadata；`statistics.total_usage` 将其与
worker usage 合并，并保留覆盖率，避免把已知小计误当成完整 cost。

每个 `subagents[]` 条目都包含版本化 `observability` 。`subagents[].verifier_count` 和 `session_verifier_count`
属于当前 session，`candidate_verifier_count` 是 candidate 累计值。需要单独查询时使用
`goal_plus_search_get_agent_observability(agent_session_id)` 。

### Terminal Dashboard 与 Report

需要持续查看项目目录时使用只读 dashboard：

```bash
./scripts/monitor_goal_plus.sh /path/to/project
./scripts/monitor_goal_plus.sh --once --goal gp_... /path/to/project
./scripts/monitor_goal_plus.sh --run run_... --no-clear /path/to/project
INTERVAL=2 RUN_LIMIT=0 ./scripts/monitor_goal_plus.sh /path/to/project
```

默认输出是一屏摘要；`--verbose` 展开 worker usage、identity、directive、handoff、advisory、artifact 和 orchestration
event summary，`--json` 输出完整组装 payload。宿主部分只读取持久 snapshot，不执行 reconciliation、wait、close 或 interrupt。

`goal_plus_search_report(run_id)` 从同一份持久 Evidence 生成 `report.md` 和 self-contained `report.html`。
关联 Goal Plus
的 run 只有在 record 已进入 `complete` 或 `abandoned` 后才能生成报告；`blocked` 是可恢复暂停，不满足报告 gate。正常顺序是
select、promote、按配置 apply、record Search result、final audit、可选 final-check、terminal status，最后为每个已记录
run 生成一次 report。

`goal_plus_record_search_result` 预留系统报告路径，不表示报告已经存在。独立 final-check
的 `goal_plus_report_lifecycle` 投影列出当前 revision 已结算 run 的 canonical 报告路径和
`linked_goals_terminal` 生成 gate；审查员先核验 Search Evidence、精确 promotion 和其他
交付物，不要求尚未到生成阶段的系统报告。这不豁免用户要求的其他文件或报告，也不把
预留路径当作完成证据。终态后仍须生成并核对实际报告，不能手工制造文件来绕过 gate。

HTML 是生成时刻的静态视图。后续产生新的 hook event 或 observability 后，需要再次调用 `goal_plus_search_report` 才会更新。缺失数据保持
`Not observed` 或列入 `unavailable_metrics` ，不会从 transcript prose 补造。

Goal Monitor 优先投影关联该 Goal 的最新 execution scope，缺失时才回退到所选 run 的
scope。Git 报告的 publication 状态和 artifact 由持久 Promotion 补全投影，不写回或修复
run。报告用例先检查关联 Goal 终态，再写 MD/HTML、执行 snapshot cleanup 并刷新
lifecycle；纯 renderer 不执行状态转换。

普通终态验收到这里为止：`host_cleanup.state=complete` 表示临时 candidate/annotation Child、private
branch 和未 pin snapshot 已按合同回收，durable `.gp` Evidence 与报告仍保留。不要遍历
`GOAL_PLUS_ROOT`、ThinkThread `instances/` 或 `host-pools/` 来猜测内部文件名。只有 monitor 报告
`needs_recovery`/`failed`，或正在开发 provider lifecycle 时，才进入这些目录做定向调试。底层
Child、Artifact View 与 cleanup 强断言由
[`tests/st_thinkthread/test_linux_v3.py`](../tests/st_thinkthread/test_linux_v3.py) 的真实系统验收负责。

## Runtime 与 Host 的边界

Goal Plus 有两层状态：

| Owner | 负责内容 |
| --- | --- |
| runtime | goal/subagent dispatch 记录、frozen spec、candidate workspace、iteration、verifier score、selection、report 和 promotion patch |
| Codex/Pi/其他 host | worker launch、wait、message、continuation、deadline、interrupt 和原生日志 |

Search runtime 不维护 worker process lifecycle。Annotation task/retry 属于 Application；
run 下的 single-flight drainer 是 host operational state，依然由 Execution 和宿主管理，
不是 runtime supervisor 的例外。完整 scope 生命周期见
[Agent Host Adapters](agent-host-adapters.md#main-owner-生命周期) 。

因此，`.gp/` 说明“runtime 接受了什么事实”，host log 说明“worker 和 supervisor 实际做了什么”。`AgentSessionRecord` 是
context/provenance handle，不是 liveness record。

## `.gp/` 持久状态

```text
.gp/
├── goal-plus/<goal_plus_id>/
│   ├── goal.json                                # 当前 goal、revision、work item 和 Search link
│   └── events.jsonl                             # append-only transition Evidence
├── specs/<frozen_spec_id>/
│   ├── frozen_spec.json                         # frozen SearchSpec
│   └── verifier_artifacts/<path>                # hash-pinned verifier 文件
├── host-scopes/<scope_id>/                     # native session/process 与 TT 资源日志
├── host-sessions/pi/                           # 本地 Pi 原生历史
├── host-pools/thinkthread-annotations/          # 独立 Annotation Child 资源
└── runs/<run_id>/
    ├── run.json                                 # RunRecord 和 current best
    ├── best.json                                # best candidate/iteration/commit 指针
    ├── plans/<plan_id>.json                     # 初始 candidate 分配
    ├── candidates/<candidate_id>/
    │   ├── candidate.json                       # iterations[]、results_ledger[] 和 score
    │   ├── task.json                            # CandidateTask snapshot
    │   ├── evidence-annotations/iteration-<n>.json
    │   └── logs/iteration-<n>-<verifier>-<id>.log
    ├── workspace/<candidate_id>/
    │   ├── .git/
    │   ├── results.tsv
    │   └── <allowed_files>
    ├── shared/index.json                        # shared-dir family/tool index
    ├── shared/tools/<candidate_id>/<iteration>/ # immutable tool snapshot
    ├── agent_sessions/<agent_session_id>.json
    ├── report.md / report.html
    └── promotion/
```

新的 `parallel_loops` 和 `adaptive_search` run 在 `plans/` 下都只有一个初始 plan，但同一 candidate 可以有多个 session 和
iteration。

### Candidate 与 Verifier Evidence

快速查看最新 run：

```bash
RUN=$(ls -td .gp/runs/* | head -1)
python3 -c "
import json
d = json.load(open('$RUN/run.json'))
print(f\"state={d['state']} candidates={d['candidates_total']}/{d.get('candidates_evaluated', 0)} best={d.get('best_candidate_id')}/{d.get('best_score')}\")
"
```

查看每个 candidate 的 iteration：

```bash
for f in "$RUN"/candidates/*/candidate.json; do
  python3 -c "
import json
d = json.load(open('$f'))
print(f\"{d['candidate_id']}: {len(d.get('iterations', []))} iterations\")
for it in d.get('iterations', []):
    print(f\"  iter {it['iteration']}: score={it['score']} failure={it.get('failure_class')} changed={it['changed_files']}\")
"
done
```

`feedback_policy=visible_to_workers` 时，失败 process verifier 向调用方返回有界 `stdout_tail`
/`stderr_tail`；完整输出保存在该 iteration 独立的 `log_paths` 中，不会被后续调用覆盖。

### Git 与 `results.tsv`

```bash
find "$RUN/workspace" -name .git -execdir git log --oneline \;
find "$RUN/workspace" -name results.tsv -exec sh -c 'echo "=== $1 ==="; cat "$1"' _ {} \;
```

仅 `autoresearch` binding 物化 `results.tsv`；`common` 只保留 canonical ledger。
`results.tsv` 由 runtime 根据 `candidate.json.results_ledger` 生成、继承并只追加。每个成功返回 report 的 verifier
调用贡献一行：

<!-- markdownlint-disable MD010 -->
```text
commit	combined_score	status	hypothesis
a1b2c3d	0.682	pass	baseline (concentric rings)
b2c3d4e	0.949	pass	hex lattice [5,4,5,4,5,3] s=0.1875
c3d4e5f	0.651	fail	switch to rectangular grid (regressed)
```
<!-- markdownlint-enable MD010 -->

`commit` 是 verifier 实际测试的 artifact head，之后追加 ledger 的 commit 保存在 `ledger_git_head` 。`keep` 后
workspace `HEAD` 通常比测试 snapshot 多一个 ledger commit；`discard`/`failure` 后 attempt commit 仍可按 hash 读取，但
workspace code 已恢复为 candidate-local best。

worker 不得手动编辑、删除或截断该文件，否则下一次 verifier 会报 `ResultsLedgerMutation` 。旧 candidate 恢复时，legacy
`.tmp/results.tsv` 和缺失的 verifier-backed iteration 会迁移为规范 ledger row。

## Host 原生日志

原始日志应放在 `.gp/host-logs/` 或其他 ignored 目录。它可能包含 prompt、tool input、command output、file content 或意外打印的
credential，绝不能提交。

关联日志时使用 `agent_session_id`、`candidate_id`、`session_handle.external_id` 和准确 `invocation_id`。

### Codex

脚本化运行可直接捕获 JSONL event stream：

```bash
mkdir -p .gp/host-logs
codex exec --json --cd "$PWD" "<goal-plus prompt>" \
  > ".gp/host-logs/codex-$(date +%Y%m%d-%H%M%S).jsonl"
```

`codex exec --json` 包含 `thread.*` 、`turn.*`、`item.*` 和 `error` event。`codex exec -o` 只保存最终答案，不能代替完整
trace；`--ephemeral` 会禁用本地 session rollout 持久化。

Codex 默认 transcript 位于：

```bash
find "${CODEX_HOME:-$HOME/.codex}/sessions" -name 'rollout-*.jsonl' -print
```

其他常用路径包括 `${CODEX_HOME:-$HOME/.codex}/session_index.jsonl`
、`${CODEX_HOME:-$HOME/.codex}/archived_sessions` 和 macOS 的 `~/Library/Logs/com.openai.codex/` 。需要
TUI log 时显式启用：

```bash
RUST_LOG=debug codex -c log_dir=./.codex-log
tail -F ./.codex-log/codex-tui.log
```

当前安装的 hook 包含 `UserPromptSubmit`、`SessionStart`、`PreToolUse`、`PostToolUse`、
`Stop`、`SubagentStop` 和 `Interrupt`，以实际安装资产及 trust 为准。解析到 Goal/Search/session
context 的自动 Stop/SubagentStop 将不含内容的诊断写入 `.gp/host-logs/codex-hook-events/`，
保存 decision、耗时、reason/error、停止原因和 ID。直接调用 `goal_plus_gate` 不产生这些记录。
缺失记录时先核对 hook 信任、runtime root、native identity 和该 event 的 context 绑定。

共同 app-server candidate 的原生 Stop 只记录准确身份与返回事实；verifier Evidence 的复用或
补验由共同 candidate delivery owner 决定。Main Stop、普通 collaboration 的 SubagentStop
和 candidate 返回分别处理，不能用旧 subagent 的 verifier-count 断言判断新 session 完成。

报告中的 Stop Hook Activity 和 Loop Agent Stop Outcomes 只依据持久 hook record、native session activity 和
candidate iteration。`polling-only`、`answer-only`、`empty-output` 等 tail label 是确定性诊断
heuristic，不代表模型意图，也不能证明因果关系。

### Pi RPC

Pi Search candidate 由共同 session 宿主启动 Pi RPC 进程。Main 使用
`goal_plus_session_run/wait/close`，调用观测读取 `goal_plus_search_get_agent_observability`，
资源事实读取 Monitor 的 execution scope。旧 `pi_search_pool_*` 模型工具已删除。

runner 命令形态为：

```bash
pi --mode rpc --approve \
  --session-dir <root>/.gp/host-sessions/pi \
  --session-id <agent_session_id> \
  -e <repo>/assets/pi/extensions/goal-plus.ts
```

Pi native session 保存在 `.gp/host-sessions/pi/`。共同调用观测按准确 native handle 和
invocation 写入既有 candidate provenance，不保存模型正文；用量缺失保持 unavailable。
controller 丢失后按 [Search 恢复](search-recovery.md) 处理，不能凭 session 文件重连未知
活跃执行。旧 `.gp/host-pools/pi/` 和 `pi-rpc-*.jsonl` 仅属于旧 worker 路径，不能作为新
session 的存活或完成依据。Search candidate、workspace 和 verifier Evidence 始终由 runtime 管理。

## 常见故障

### Worker 已结束但 Candidate 没有分数

先比较 `agent_sessions/<id>.json.counters.verifier_runs` 和 `candidate.json.iterations` ，再检查对应 host log
是否调用过 `goal_plus_search_run_verifier` 。worker 从未提交 verifier 时，main 可以对其留下的 workspace 调用 parent fallback
verifier，但必须省略 `agent_session_id` 。

### Agent 用 Bash 绕过 MCP Verifier

host log 中出现 `python evaluator.py` 等命令，但 runtime 没有对应 iteration，说明它只是本地诊断，
尚未通过正式 verifier 结算。不要从 shell output 手工补分；需要正式 Evidence 时让 candidate
调用 `goal_plus_search_run_verifier`。

### Subagent 仍在运行但需要停止

中断属于 host：本地 Codex/Pi 使用共同 `goal_plus_session_interrupt` 中断当前调用，
`goal_plus_session_stop/close` 停止或关闭执行资源。Search runtime 没有额外 abort API。
若 verifier 已确认失效，应先 `goal_plus_search_invalidate_run` fence run，再停止全部 worker 并由
scope 确认资源退出，最后修复 verifier、重新 freeze 并创建 successor run。

### `EditSurfaceViolation`

检查最新 iteration 的 `touched_denied_files` 、`changed_outside_allowed` 和 `changed_files` 。不要修改 frozen
spec 或手工 reset 来绕过 edit surface。

### `VerifierWorkspaceSideEffect`

检查 verifier result 的 `metrics.verifier_workspace_side_effects`
、`cleanup_failures`、`infrastructure_failure` 和 `candidate_action`。该错误表示 verifier 修改了
artifact 原有文件、验证输出清理失败，或在 preflight/promotion 的非一次性 workspace 中产生变化。
普通 candidate verifier 在一次性 worktree 中产生的纯新增文件清理成功后不触发该错误，路径记录
在 `verifier_workspace_outputs_cleaned`。

worker 应立即停止并报告。main 修复 source-owned verifier，使临时输出使用
`GOAL_PLUS_VERIFIER_TMPDIR`、`TMPDIR` 或 `tempfile`；并发 verifier 不能共享固定 `/tmp` 路径。
随后重新 freeze 并创建新 run，复用任何 candidate state 前先确认 `cleanup_failures` 为空。

## 诊断工具与副作用

| 工具 | 内容 |
| --- | --- |
| `goal_plus_monitor_snapshot(...)` | Goal/subagent dispatch、Search、session、host 和 feature plugin 的组合视图 |
| `goal_plus_search_status(run_id)` | run state、candidate count 和 best score |
| `goal_plus_search_list_history(run_id, top_n, sort_by)` | candidate ranking 和研究汇总 |
| `goal_plus_search_list_iterations(agent_session_id)` | 当前 candidate 可能很大的完整 verifier history |
| `goal_plus_search_get_agent_context(agent_session_id)` | 授权 context 初始化，可能初始化 ledger；不是纯只读查询 |
| `goal_plus_search_get_global_evidence(agent_session_id)` | 当前可见 Evidence/View，并记录读取审计和执行既有 reconciliation |
| `goal_plus_search_get_evidence_detail(agent_session_id, candidate_id, iteration)` | 按需 supplemental evaluation |
| `goal_plus_search_get_agent_observability(agent_session_id)` | 单 session 的归一化 model、timing、usage 和 artifact Evidence |

一个典型的跨层结论是：“host session 已结束，host log 中没有 `goal_plus_search_run_verifier` ，同时 `candidate.json.iterations`
为空。”这能证明 worker 没有自行结算；仅看到 assistant 声称已经验证，不能替代这两层 Evidence。
