# Meta-tool / BATE

BATE（Branch-Aware Template Extraction，分支感知模板提取）从重复出现的完整
工具调用序列中提取组合工具。它检查数据流、参数绑定和支持证据，再编译为可以执行的
`$input` / `$step` 模板。Static 使用固定目录；Online 随合格轨迹积累更新目录。

本目录独立安装，不依赖其他算法仓库。Python 包名为 `bate`，发行包名仍为
`meta-tool-online`；没有 `awo` 兼容桥接。

## Pi / Codex：统一一条命令安装

需要 Linux/macOS、Python 3.11+。Pi 还需要 Node.js 22.18+、npm 和已安装的 Pi；
Codex 需要支持项目级本地插件的客户端。安装器不负责安装宿主本身。
从本插件目录运行，将 `/绝对路径/你的项目` 替换为要使用 meta-tool 的项目：

```bash
# 选择其中一条；也可以用 bash install.sh，参数相同
python3 install.py --pi --project /绝对路径/你的项目
python3 install.py --codex --project /绝对路径/你的项目

# 同一项目同时安装两个宿主
python3 install.py --pi --codex --project /绝对路径/你的项目

# 只预览目标，不安装依赖、不写文件
python3 install.py --pi --codex --project /绝对路径/你的项目 --dry-run
```

通过 `--pi` / `--codex` 选择宿主；不选宿主时默认为 Pi，不传 `--project` 时安装到当前目录。
两个宿主都使用**项目级安装**，不改用户全局模型、API key 或信任设置。
Codex 使用 Skill＋MCP 入口。

命令自动创建 `.meta-tool/.venv/`，安装 BATE Python 包；Pi 还会复制插件并运行
`npm ci --ignore-scripts`，Codex 会安装 MCP 依赖并生成插件清单。
不需要先手动执行 `pip install -e '.[dev]'` 或 `npm ci`。安装需要访问 Python/npm
包源，不调用模型；现有的 Pi Online、批读和执行策略保持不变。
源码修改后，先关闭目标项目中的 Pi/Codex 会话，再运行同一安装命令更新副本。
重复安装不会重复注册；已安装副本不依赖原 checkout。可以分两次安装两个宿主，
但共用同一个 Python 环境，升级任意一端也会更新共用的 BATE 包。

### Codex 安装产物

如果当前 Python 环境已经安装了 `.[codex]`，可以直接：

```bash
python -m bate.codex install --project /绝对路径/你的项目
```

安装生成：

| 位置（相对于目标项目） | 用途 |
| --- | --- |
| `.meta-tool/.venv/` | 两个宿主共用的项目内 Python 环境 |
| `.bate-codex/config.json` | BATE 配置：读取根目录、学习输入及启动默认值 |
| `.bate-codex/plugin/` | Codex 插件清单、MCP 注册信息和 `meta-tool` Skill |
| `.bate-codex/catalog.json` | 默认空种子目录；内置批读不需要历史轨迹 |
| `.agents/plugins/marketplace.json` | 项目本地插件目录，保留其他插件 |
| `.codex/config.toml` | 增加有标记的本地 marketplace 和插件启用配置，保留其他设置 |

不修改用户全局 Codex 设置、模型或 API key。已有同名但非本安装器管理的配置会报冲突，
不会覆盖。新建的 marketplace 名称包含项目路径摘要，避免不同项目共用同一个
本地插件缓存身份；若接入已有 marketplace，应确保其名称不与其他独立项目重复。
Codex 刷新插件时可能在自己的缓存中复制插件，但安装器不会改全局设置。
`.meta-tool/` 和 `.bate-codex/` 自带忽略规则；生成的配置与插件清单含本机路径，
不要作为可移植配置直接提交给其他用户。Codex 只加载已信任项目的配置；安装器不修改信任策略。

## Codex：在会话中使用

在目标项目开启新会话（必要时重启客户端刷新本地插件）。桌面端输入 `/`，选择
`meta-tool` 技能；CLI 可以用 `/skills` 选择，或显式输入 `$meta-tool`。
插件名可能由客户端加命名空间，以菜单中的名字为准。这里没有修改 Codex 的内置
斜杠命令解析器，因此不承诺所有客户端都接受裸 `/meta-tool` 字符串。

新安装默认处于关闭状态，选择技能后输入操作：

```text
$meta-tool status       查看状态；不启用
$meta-tool on           启用组合执行和批读，保持当前模式
$meta-tool static       启用执行，冻结当前目录，暂停在线学习
$meta-tool online       启用执行和在线学习（需要事先配置轨迹）
$meta-tool off          关闭组合执行和在线学习
$meta-tool batch off    只关闭内置批读
$meta-tool batch on     恢复内置批读
```

Skill 会调用真正的 `meta_tool_control` 接口，再反馈返回状态；文字回答不算启动成功。
入口通过模型转调 MCP，会产生模型处理；不是 Pi 的直接命令回调。
默认目录为空，但启用后立即有 2/3/4 段项目批读可用。
通过 `/mcp` 检查 BATE 连接；`bate_catalog` 查看目录、启用状态和学习进度。

开关作用于**当前 MCP 连接**，不写回配置、不改变其他连接。`off` 等待已经在运行的
执行或提取完成，返回后阻止新执行和学习；不会删除已有目录和轨迹。
切换 Static 保留本连接已学习的目录；切回 Online 继续处理新证据。
服务重启恢复 `start_enabled` 和 `mode` 配置；默认安装重新回到关闭的 Static。
若客户端复用同一 MCP 连接，连接内开关也会被复用，不应把它当成全局开关。

安装时也可以接入已有 BATE 配置，而不使用默认文件服务：

```bash
python3 install.py --codex --project /绝对路径/你的项目 \
  --config /绝对路径/已有的BATE配置.json
```

## Online 的轨迹来源

安装时可以指定已有或将来写入的 rollout 文件：

```bash
python3 install.py --codex --project /绝对路径/你的项目 \
  --trace-file /绝对路径/session-a.jsonl \
  --trace-file /绝对路径/session-b.jsonl
```

也可以在 `.bate-codex/config.json` 的 `online.trace_files` 中设置明确文件路径后
重启服务。没有轨迹来源时，`online` 操作会返回 `online_sources_not_configured`，
保留之前的启用状态和模式。它不会自动扫描所有历史或猜测当前 session 的日志。

学习只覆盖配置中绑定的只读 MCP primitives；原生 shell/patch 仍不参与提取。
默认文件服务用于批读，不会把批读自身调用当成新学习证据。要学习其他工作流，
需在 `servers` / `bindings` 中配置相应只读 MCP，并提供匹配的原始调用轨迹。
每条候选仍需至少 3 段支持轨迹、2 个独立 session，以及原有数据流和编译检查。
完成 turn 不代表任务评测成功。内置批读与 Online 新提取工具分别统计。

同一 `online.state_dir` 同时只允许一个学习进程；多个独立 Codex 连接并行学习时
使用不同状态目录。学习开关不会绕过这个锁。

## 默认批读的范围

默认安装提供一个独立的只读 MCP 文件服务，读取根目录固定为安装时指定的项目。
这是明确绑定的文件服务，不是对 Codex 原生文件工具的拦截。

- 每批 2～4 段，每段最多 300 行；所有路径和范围提前确定。
- 每段返回最多 64 KiB，并保留截断标记；仅读取不超过 8 MiB 的 UTF-8 普通文件。
- 拒绝项目外路径、符号链接、设备文件，以及 `.git`、`.codex`、`.agents`、
  `.bate-codex`、`.meta-tool`、`.pi`、`.aws`、`.env*`、`credentials`、`secrets`。
- 不包含 shell、写文件或目录扫描能力。文件服务受操作系统权限和上述根目录限制，
  不等同于 Codex 原生工具的沙箱。

若下一个读取范围需要看完前一个结果才能决定，使用普通读取后再判断。
批读节省的是可能的模型往返，不保证减少每个任务的 token 或墙钟时间。

## Pi 使用

从本插件目录安装，再进入目标项目正常启动 Pi：

```bash
python3 install.py --pi --project /绝对路径/你的项目
cd /绝对路径/你的项目
pi
```

安装器向 `.pi/settings.json` 的 `packages` 增加一条本地包注册，保留其他插件及模型
设置。Pi 按自己的项目信任机制加载它；不需要再传 `--extension`，也不要带
`--no-extensions`。不要同时在全局配置或启动参数加载另一份 meta-tool。

| 位置（相对于目标项目） | 用途 |
| --- | --- |
| `.meta-tool/.venv/` | BATE 算法及 Python 依赖 |
| `.meta-tool/integrations/pi-meta-tool/` | Pi 插件副本、npm 依赖、worker 包装器及 Python 桥接 |
| `.pi/settings.json` | 注册此项目的 Pi 包 |
| `.pi/meta-tool/config.json` | 指向已安装 Python 和桥接；保留原 Online、准入等配置 |

如果已有另一份 meta-tool 注册或自定义 `pythonCommand` / `metaToolRoot` /
`bridgePath`，安装器报冲突，先检查并移除相应旧设置再重试。配置含本机绝对路径；
移动目标项目后需要重新配置安装路径。配置和学习轨迹不要直接提交到代码仓库。

Pi 扩展加载后，在 session 内使用：

```text
/meta-tool                 查看状态
/meta-tool enable          启用组合执行
/meta-tool disable         禁用组合执行
/meta-tool online status   查看在线提取进度
/meta-tool online run      手动触发提取
/meta-tool extract         提取当前轨迹中的组合
```

Pi 新工作区默认启用；自动 Online 学习由 `online.enabled` 控制。
Pi 的执行开关与自动学习配置分开，不能把 `/meta-tool disable` 理解为停止全部轨迹采集。
Goal-Plus worker 传播、私有运行目录和批读配置见 [Pi 说明](integrations/pi-meta-tool/README.md)。

## 验证与更多说明

```bash
python -m pip install -e '.[dev,codex]'
python -m pytest
```

安装器与真实 Pi 加载检查（只对一次性测试项目运行）：

```bash
python3 tests/smoke_host_install.py --project /绝对路径/已安装的测试项目 --pi /绝对路径/pi
```

该检查验证安装后的自动加载、`/meta-tool` 注册、主控/worker 的 2/3/4 段批读和
Python 提取桥接；测试扩展禁止模型请求。需要先用统一安装器为测试项目安装 Pi。

测试使用合成轨迹和本地 MCP，不调用模型。
[Codex 详细说明](integrations/codex/README.md)覆盖手动注册、审核与轨迹格式；
[提取流程](docs/extraction.md)解释候选与编译；
[调度策略](docs/safe-dispatch.md)解释准入和失败边界。

客户端入口依据：[技能与斜杠菜单](https://learn.chatgpt.com/docs/reference/slash-commands)、
[项目级插件安装](https://developers.openai.com/plugins/build/plugins)。
