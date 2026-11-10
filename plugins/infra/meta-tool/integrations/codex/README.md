# Codex: Static / Online

## 快速安装与会话入口

从插件根目录运行：

```bash
python3 install.py --codex --project /absolute/path/to/your/project
```

与 Pi 共用根目录安装入口（`--pi` / `--codex`，也支持 `bash install.sh`）。
这会创建项目本地 Skill＋MCP 插件、`.meta-tool/.venv/` Python 环境和有界只读文件服务。
新会话在 `/` 菜单选择 `meta-tool`，或使用 `$meta-tool on` / `off` /
`static` / `online` / `status`。CLI 也可通过 `/skills` 选择。
新安装默认关闭；裸 `/meta-tool` 是否被识别取决于客户端菜单，未修改 Codex 内置命令。
安装不修改全局设置或项目信任。完整步骤见 [中文说明](../../README_zh.md)。

Skill 调用 `meta_tool_control` 控制本 MCP 连接；关闭会阻止新执行和学习，
等待在途工作结束。Static 保留当前目录并暂停学习。状态在服务重启后恢复配置默认值。
Online 必须先通过安装参数 `--trace-file` 或配置给出明确轨迹来源，默认不扫描历史。

手动配置也可设置 `session_control: true` 暴露控制工具，设置 `start_enabled: false`
使服务等待显式启用。原手动配置不设置这两项时，仍保持旧的工具列表和启动行为。
设置了控制入口的服务使用固定 search/run 接口，切换模式无需刷新模型工具列表。

## 手动 MCP 接入

独立的 Codex MCP 适配层，调用本插件的 BATE 提取、编译和运行时，不依赖 Pi。
支持上游 **stdio MCP 的只读 primitives**，不模拟原生 `exec_command`、
`apply_patch` 或它们的权限。适配器自身不调用模型，不需要模型 API key。

## 安装与 Static 演示

从 `plugins/infra/meta-tool/` 执行，使用 Python 3.11+：

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[codex]'
python -m bate extract --traces tests/fixtures/branched_traces.json \
  --config tests/fixtures/branched_config.json --out /tmp/bate-codex-demo \
  --also-raw --require-fully-bound --naming stable-short --max-len 4
python -m bate.codex check --config integrations/codex/demo.config.json
```

演示配置的 `python3` 必须解析到已安装 MCP extra 的解释器；实际配置建议写绝对
解释器路径。合成演示有两个固定指纹的模板；`check` 只连接并检查上游 tools/list，
不执行 primitives，也不启动在线提取。配置及上游进程必须可信。

在自己的 Codex 中注册（此命令会修改 Codex 配置）：

```bash
codex mcp add bate -- /absolute/path/to/.venv/bin/python -m bate.codex serve \
  --config /absolute/path/to/demo.config.json
```

该接入使用 [Codex 的标准 MCP 机制](https://developers.openai.com/codex/mcp)。
在新会话查看 `/mcp`，先调用 `meta_tool_search`，再将返回的名称、完整 fingerprint
和符合 input_schema 的输入交给 `meta_tool_run`。Static 还保留每个已审核
`mt_xxx` 的直接入口。`bate_catalog` 提供目录和诊断。

真实 Static 使用 [config.example.json](config.example.json)。先运行
`bate-codex review --catalog /path/to/active_meta_tools.json`，检查步骤及输入合同，
再把认可的名称和完整指纹填入 `approved_meta_tools`。review 本身不批准工具。

## Online 使用

复制 [online.config.example.json](online.config.example.json)，填入实际的上游
stdio MCP、primitive 绑定、允许读取的 rollout 文件和独立状态目录。注册命令相同，
只需将 `--config` 换成该配置。`catalog: null` 从空池开始；也可指定已审核的种子
目录。模型工具参数不能修改配置。

流程：指定的 JSONL → 完成 turn 封存 → 去重和只读范围过滤 → 同一 BATE 提取器 →
session/数据流/编译检查 → 版本写入与原子发布 → search/run 使用新目录。
默认每 2 秒检查；catalog/search 请求也会处理新证据。大轨迹提取可能较慢，可调整
`extraction_timeout_seconds` 并相应配置客户端的 MCP 工具调用超时。

- `trace_files` 为 1–64 个明确文件路径，不扫描整个历史目录。可以预先指定尚未
  创建的文件。跨会话积累使用同一文件列表和状态池；增加路径需更新配置并重启服务，
  不自动猜测当前 session 文件。
- 只接纳 `task_complete` / `turn.completed` 之前已配对完成的 primitives。
  未结束/失败 turn、损坏的完整 JSON 行、缺失结果、重复调用身份不用于学习。
  正在写入的末尾半行推迟处理。`success` 保持未知：完成 turn 不等于任务评测通过。
- 默认至少 3 个 distinct trace 片段、2 个独立 session。同 session 的多个片段
  不冒充多个 session；文件重读、重复副本和重启均不倍增 support。
- 示例中的 `auto_admit_read_only: true` 明确允许机器准入新提取的只读模板；
  未设置时默认 false，只产出候选，执行仍需 `approved_meta_tools` 指纹许可。
  机器准入不是人工审核，也不允许写操作。
- 未知工具、原生 shell/patch 和 meta-tool 调用形成边界，不删掉中间未知工具
  再拼接两边，也不拿组合工具自身的重复调用提高 support。
- `state_dir/current.json` 把输入摘要、版本和目录一起原子发布；`versions/`
  保留版本。一个池只允许一个写进程。失败不推进 checkpoint，保留旧目录并允许
  重试；空提取不清空旧目录。无法识别的状态格式显式报错。
- Online 固定提供 search/run，不依赖客户端刷新 tools/list。run 要求完整指纹，
  单次执行固定模板，不在中途替换。配置中的种子模板在同名时优先，未审核的覆盖项
  不继承自动许可。

这是只读 MCP Online 适配，不是 Pi 全工具 hook 的等价移植。不透明捕获原生 shell
内部调用，也不新增写操作审批或 HTTP/OAuth 上游支持。内置数、候选数、准入数、
调用数需分别统计；工具数量不能证明 token 或时间收益。

## 默认批读

Static / Online 的 `batch_enabled` 默认为 true。显式配置名为 `read` 的只读
binding，指向接受 `path`、`offset`、`limit` 的上游 MCP 工具时，提供 2/3/4 段
模板及 `meta_tool_read_batch`。这不是 Codex 原生读文件工具；缺少该 binding 时
不暴露批读入口，也不自行打开本地文件。

```json
{
  "ranges": [
    {"path": "src/a.py", "offset": 1, "limit": 40},
    {"path": "src/b.py", "offset": 10, "limit": 60}
  ]
}
```

每段最多 300 行，所有范围事先给定，结果顺序返回。设置 `batch_enabled: false`
关闭。内置模板是预置能力，单独计数，不伪装成新学成果。

## 数据与执行边界

离线导入：`bate-codex import-traces --input a.jsonl b.jsonl --out traces.json`。
支持 rollout 调用/结果对及 `codex exec --json` 完整 MCP 事件；不猜造缺失参数、
不执行自由格式 JavaScript，不自动去掉工具名前缀。串行请求轮次只作为可观察证据，
不是隐藏 LLM 响应次数的精确计量。

catalog、轨迹、状态目录和上游 cwd 相对配置文件解析，不展开 `~` 或变量。上游
凭据使用 `env_vars` 白名单传入。轨迹/状态是本地私有数据，可能包含代码和工具结果，
不要自动发布到仓库。

binding 必须 `read_only: true`，上游必须 `readOnlyHint: true` 且不能 destructive。
声明不是沙箱，需使用可信上游。可用 `input_schema_sha256` 固定 schema；每次执行
还会与启动时看到的 schema 比较。无效输入在第一步之前拒绝；执行失败立即停止，
无自动重试/fallback。stdout 专用于 MCP JSON-RPC，诊断不回显敏感参数。

## 验证

```bash
pip install -e '.[dev,codex]'
python -m pytest tests/test_codex_traces.py tests/test_codex_mcp.py tests/test_codex_online.py
```

覆盖真实 stdio 握手、空池追加轨迹后在线提取/执行、去重/重启、损坏输入、发布失败
重试、写进程互斥、schema 漂移和失效指纹。使用合成工具和轨迹，不调用模型，
不宣称经过真实任务准确率/性能回归。Online 状态锁当前要求 Unix `flock`。
未安装 MCP extra 时协议测试跳过。
