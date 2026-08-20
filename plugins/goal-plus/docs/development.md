# Goal Plus 开发指南

Goal Plus 的开发内容包括共享的 Python runtime，以及 Pi、Codex 两套宿主集成。
开发者需要从本地 Muyuan checkout editable 安装 runtime，再将待调试的宿主连接到
同一个 checkout。修改代码后，应根据文件所属的加载边界刷新宿主，并在新进程或
新线程中验证改动。

## 代码与加载边界

开发前先确认改动属于哪一部分：

| 开发内容 | 源码位置 | 加载方式 |
|---|---|---|
| Python runtime | `src/goal_plus/` | Pi、Codex 通过当前 Python 环境中的 `goal-plus` 命令加载 editable checkout |
| Pi package | `package.json`、`.pi/extensions/`、`.pi/skills/` | Pi 直接读取已注册的 checkout 路径 |
| Codex skill | `.codex/skills/` | 同步到顶层 `skills/` 后，由开发态 plugin staging 副本加载 |
| Codex hook | `.codex/hooks.example.json` | 同步到 `hooks/hooks.json` 后，由开发态 plugin staging 副本加载 |
| 其他 Codex plugin 资产 | `.mcp.json`、`.codex-plugin/plugin.json` | 由开发态 plugin staging 副本加载 |

Python runtime 使用 editable 安装，因此修改 `src/goal_plus/` 后通常无需重新安装。
Pi 直接读取 checkout 中的宿主资产；Codex 的静态资产则需要重新复制到开发态
plugin。两者都不会修改已经启动的进程或线程中已加载的旧内容。

---

## 建立基础开发环境

为当前 Goal Plus checkout 建立独立的 Python 环境：

```bash
cd /absolute/path/to/muyuan/plugins/goal-plus
python -m venv /absolute/path/to/goal-plus-venv
source /absolute/path/to/goal-plus-venv/bin/activate
python -m pip install -e ".[dev]"
```

确认 runtime 来源正确：

```bash
python -c 'import goal_plus; print(goal_plus.__file__)'
command -v goal-plus
```

`goal_plus.__file__` 应位于当前 checkout 的 `src/goal_plus/` 下，`goal-plus` 命令
应来自刚激活的 Python 环境。后续安装、刷新和启动 Pi 或 Codex 时，都应继续使用
这个环境。

---

## 开发 Pi 集成

### 将 Pi 连接到 Checkout

Pi 的开发态 package 直接注册本地源码路径：

```bash
source /absolute/path/to/goal-plus-venv/bin/activate
pi install /absolute/path/to/muyuan/plugins/goal-plus
pi list
```

`pi list` 应显示当前 checkout 的绝对路径。此后 Pi 会从该路径读取
`package.json`、extension 和 skill，不会创建需要单独刷新的资产副本。

### 开发循环

开发 Pi 集成时，按以下流程进行：

1. 修改 `src/goal_plus/`、`.pi/extensions/`、`.pi/skills/` 或
   `package.json`；
2. 运行与改动对应的自动测试；
3. 结束正在运行的 Pi 进程；
4. 在同一 Python 环境中启动新的 Pi 进程；
5. 从目标 workspace 执行一次真实的 `/goal-plus` 调用。

修改 Python runtime 或 Pi 资产后都不需要重复执行 `pi install`：前者由 editable
安装指向 checkout，后者由 Pi 直接读取 checkout。只有 checkout 路径发生变化或
Pi 的 package 注册需要修复时，才重新注册：

```bash
pi remove /absolute/path/to/old/muyuan/plugins/goal-plus
pi install /absolute/path/to/muyuan/plugins/goal-plus
```

### 验证 Pi 改动

Pi package、extension 或 skill 变更至少运行：

```bash
python -m pytest -q tests/test_pi_assets.py
```

涉及 worker、pool 或 RPC 时，继续运行对应的 `tests/test_pi_*.py`；需要验证真实
宿主行为时，运行 `tests/st_pi/` 中相应的集成测试。

完成自动检查后，从实际目标 workspace 启动新的 Pi 进程：

```bash
cd /absolute/path/to/target-workspace
source /absolute/path/to/goal-plus-venv/bin/activate
pi -p "/goal-plus 检查当前工作区并给出简短结论"
```

这一步用于确认新 extension、skill 和 Python runtime 已在同一次宿主调用中生效。

---

## 开发 Codex 集成

### 安装开发态 Plugin

Codex 不直接读取 checkout 中的静态 plugin 资产。开发脚本会把当前资产复制到
`${CODEX_HOME:-~/.codex}/local-marketplaces/goal-plus`，并安装为固定的开发态
标识 `goal-plus@goal-plus-local`：

```bash
source /absolute/path/to/goal-plus-venv/bin/activate
cd /absolute/path/to/muyuan/plugins/goal-plus
python scripts/install_codex_plugin.py --skip-runtime-install
codex plugin list
```

开发环境中只应启用 `goal-plus@goal-plus-local`。如果同时存在
`goal-plus@goal-plus`，Codex 可能重复加载同一组 skill、hook 和 MCP 配置，应先
删除非开发态注册：

```bash
codex plugin remove goal-plus@goal-plus
codex plugin marketplace remove goal-plus
```

安装完成后，新建 Codex 线程，在 `/hooks` 中检查并信任 Goal Plus；再新建一个
线程，使全部 handler 从 `SessionStart` 开始加载。

### 开发 Python Runtime

只修改 `src/goal_plus/` 时，不需要刷新 plugin，也不需要重复 editable 安装。
结束当前 Codex 线程并新建线程，使新的 MCP server 和 hook 命令加载 checkout 中的
最新 Python 代码。

### 开发 Codex Skill

`.codex/skills/` 是 Codex skill 的开发源码，顶层 `skills/` 是安装到 plugin 的
发布副本。修改 skill 后先同步两者，再刷新开发态 plugin：

```bash
cd /absolute/path/to/muyuan/plugins/goal-plus
python scripts/sync_codex_plugin_assets.py
python scripts/sync_codex_plugin_assets.py --check
python scripts/install_codex_plugin.py --skip-runtime-install
```

刷新完成后，在新建的 Codex 线程中验证 skill。

### 开发 Codex Hook

`.codex/hooks.example.json` 是项目本地 hook 模板，`hooks/hooks.json` 是 plugin
使用的副本。修改 hook 后同步副本并刷新 plugin：

```bash
cd /absolute/path/to/muyuan/plugins/goal-plus
cp .codex/hooks.example.json hooks/hooks.json
python scripts/install_codex_plugin.py --skip-runtime-install
```

hook 内容变化会产生新的信任哈希。新建线程后，在 `/hooks` 中重新检查并信任，
然后再新建一个用于测试的线程。

### 开发其他 Codex Plugin 资产

修改 `.mcp.json`、`.codex-plugin/plugin.json` 或其他顶层 plugin 资产后，刷新
开发态 plugin：

```bash
python scripts/install_codex_plugin.py --skip-runtime-install
```

安装脚本会生成新的 cachebuster，并替换当前 `CODEX_HOME` 中的
`goal-plus@goal-plus-local` staging 副本；仓库中的正式版本号不会改变。静态资产
刷新后必须在新建的 Codex 线程中验证。

### 验证 Codex 改动

Codex skill、hook 或 plugin 资产变更至少运行：

```bash
python -m pytest -q tests/test_codex_assets.py tests/test_codex_doctor.py
```

随后从目标 workspace 检查实际开发态安装：

```bash
cd /absolute/path/to/target-workspace
source /absolute/path/to/goal-plus-venv/bin/activate
goal-plus-codex-doctor --project-root .
```

最后在完成刷新和 hook 信任的新线程中运行最小 `$goal-plus` 任务，确认当前加载的
skill、hook、MCP server 和 Python runtime 均来自正在开发的 checkout。

---

## 改动后的更新动作

| 修改内容 | Pi | Codex |
|---|---|---|
| `src/goal_plus/**/*.py` | 重启 Pi 后验证 | 新建线程后验证 |
| `.pi/extensions/**`、`.pi/skills/**`、`package.json` | 重启 Pi 后验证；无需重新 `pi install` | 无需刷新 |
| `.codex/skills/**` | 无需刷新 | 同步 skill、刷新 plugin、新建线程 |
| `.codex/hooks.example.json`、`hooks/hooks.json` | 无需刷新 | 同步 hook、刷新 plugin、重新信任、新建线程 |
| `.mcp.json`、`.codex-plugin/plugin.json`、其他顶层 plugin 资产 | 无需刷新 | 刷新 plugin、新建线程 |
| Python 依赖或 `pyproject.toml` | 重新 editable 安装并重启 Pi | 重新 editable 安装、刷新 plugin 并新建线程 |

一次改动涉及多个加载边界时，应合并执行相应动作。例如同时修改 Python runtime
和 Codex hook，需要刷新 plugin、重新信任 hook，并在新线程中验证两部分改动。

---

## 隔离并行开发环境

每位开发者或每个并行开发版本都应使用独立的 checkout、Python 环境和目标
workspace。不同操作系统账号的 Pi、Codex 全局配置由各自主目录隔离；如果多个
版本共用同一账号，还必须分别设置：

- `CODEX_HOME`；
- `PI_CODING_AGENT_DIR`。

```bash
source /absolute/path/to/developer-a/venv/bin/activate
export CODEX_HOME=/absolute/path/to/developer-a/codex-home
export PI_CODING_AGENT_DIR=/absolute/path/to/developer-a/pi-agent
```

安装、刷新和启动宿主时必须保持这些环境一致。Codex 的开发态标识固定为
`goal-plus@goal-plus-local`，同一个 `CODEX_HOME` 中后一次刷新会替换前一个版本；
Pi 的 package 注册和模型配置则归属于当前 `PI_CODING_AGENT_DIR`。仅指定源码路径
不能隔离共享的 Python `PATH` 或宿主注册信息。

Goal Plus 运行状态保存在目标 workspace 的 `.gp/` 中，agent 修改也直接作用于该
workspace。因此，并行开发还需要使用不同的目标目录或 Git worktree。

## 相关文档

- [Pi 集成](pi.md)
- [Codex 集成](codex.md)
- [Runtime 状态调试](debugging-runtime.md)
