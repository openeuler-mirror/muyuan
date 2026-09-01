# Goal Plus 开发指南

Goal Plus 由共享 Python runtime、Pi package 和 Codex plugin 组成。仓库只提供一个统一
安装入口：`install.sh`。Codex 使用正常安装；Pi 另外提供 checkout-local 开发态，便于在不
切换 Pi 账号配置的情况下测试当前源码。

## 安装模式

| 命令 | Python runtime | 宿主资产 | 宿主配置 |
| --- | --- | --- | --- |
| `./install.sh --codex` | 非 editable，安装到所选 Python | 注册当前 checkout 的正常 Codex plugin | 写正常 Codex marketplace/plugin 注册 |
| `./install.sh --pi` | 非 editable，安装到所选 Python | 注册当前 checkout 的 Pi package | 写 Pi 用户 package 注册 |
| `./install.sh --pi-dev` | editable，安装到 `.goal-plus-dev/venv` | launcher 显式加载当前 Pi extension 和 skill | 不修改 Pi 用户配置 |

`--pi` 和 `--pi-dev` 互斥；`--codex --pi` 与 `--codex --pi-dev` 都有效。正常安装使用
当前 shell 的 `python3`。先激活目标环境；也可以显式选择该环境的解释器，但它的 scripts
目录仍须位于 `PATH`：

```bash
PYTHON=/absolute/path/to/python ./install.sh --codex --pi
```

安装器会先检查同名宿主资源。来源不同、旧版 staging 安装或散落的同名 Pi asset 都会
使安装失败，并显示冲突来源；不要通过复制、覆盖或修改宿主账号目录绕过检查。

## 开发 Pi 集成

### 建立开发环境

在当前 Goal Plus checkout 中运行：

```bash
./install.sh --pi-dev
source .goal-plus-dev/set_env.sh
```

安装器创建：

- `.goal-plus-dev/venv`：editable 安装当前 checkout 和开发依赖；
- `.goal-plus-dev/bin/pi-goal-plus`：固定到当前 checkout 的 Pi launcher；
- `.goal-plus-dev/set_env.sh`：只把 launcher 目录加入当前 shell 的 `PATH`；
- `.goal-plus-dev/installation.json`：标识该目录由哪个 checkout 管理。

查看安装器已登记及当前 Git worktree 中的 Pi 开发环境、源码版本和总空间占用：

```bash
./install.sh --env-show
```

成功的 `--pi-dev` 安装会在用户 state 目录写入一条仅含环境路径和 source 路径的轻量
registry 记录；它不包含 Pi 配置、模型或认证信息。已删除的环境会显示为 `missing`，不会被
命令自动清理。

`set_env.sh` 不设置 `CODEX_HOME`、`PI_CODING_AGENT_DIR`，也不把 venv 的 `bin` 全局加入
当前 shell。`pi-goal-plus` 启动时才临时设置 runtime `PATH`，验证 `goal_plus` 模块确实来自
当前 checkout，然后通过 Pi 的 `--extension` 和 `--skill` 参数显式加载：

```text
.pi/extensions/goal-plus.ts
.pi/skills/goal-plus/SKILL.md
```

当前 Pi package 只有这一个面向用户的 skill。Search 是 Goal Plus 内部模式，不是第二个
用户级 Pi skill。

### 从目标 workspace 启动

```bash
cd /absolute/path/to/target-workspace
pi-goal-plus
```

用户级或目标项目若已注册同一 checkout，Pi 会按绝对资源路径去重，开发 launcher 可以直接
使用。安装时和 launcher 每次启动时仍会拒绝来自其他 checkout 或复制路径的 Goal Plus；若
报错，先按报告的 source 执行 `pi remove <source>`。断链的旧式 loose-resource symlink 不会被
Pi 加载，也不会阻塞开发态。其他 Pi package、模型设置和认证信息不会被修改。

### Pi 开发循环

1. 修改 `src/goal_plus/`、`.pi/extensions/`、`.pi/skills/` 或 `package.json`；
2. 依赖或 `pyproject.toml` 变化时重跑 `./install.sh --pi-dev`；
3. 运行对应自动测试；
4. 结束旧 Pi 进程；
5. 从目标 workspace 重新运行 `pi-goal-plus`；
6. 执行一次真实的 `/goal-plus` 调用。

Pi package、extension 或 skill 变更至少运行：

```bash
python -m pytest -q tests/test_install.py tests/test_pi_assets.py
```

涉及 worker、pool 或 RPC 时继续运行对应 `tests/test_pi_*.py`；只有具备 Pi credentials
和所需 provider 时才运行 `tests/st_pi/`。

## 开发 Codex 集成

### 只使用正常 Plugin

Codex 不再提供 `goal-plus@goal-plus-local`、cachebuster staging 或 checkout-local profile。
从当前 checkout 安装或更新时统一运行：

```bash
./install.sh --codex
```

这会安装当前 Python runtime，并保留唯一的正常来源 `goal-plus@goal-plus`。安装或代码更新后
新建 Codex thread；hook 内容变化时，在 `/hooks` 中重新检查并信任，再新建一个 thread。

若旧开发安装仍存在，先显式删除注册：

```bash
codex plugin remove goal-plus@goal-plus-local
codex plugin marketplace remove goal-plus-local
```

确认 `${CODEX_HOME:-~/.codex}/local-marketplaces/goal-plus` 确实是已退休 installer 生成的目录
后，才由开发者自行删除。统一安装器不会递归删除用户目录。

### 修改 Codex 资产

`.codex/skills/` 是 skill 的开发源码，顶层 `skills/` 是 plugin 发布面。修改 skill 后同步：

```bash
python scripts/sync_codex_plugin_assets.py
python scripts/sync_codex_plugin_assets.py --check
```

`.codex/hooks.example.json` 是 hook 的开发源码模板，`hooks/hooks.json` 是 plugin 发布面；修改时
保持两者一致，但前者不是另一条安装路径。`.mcp.json` 和 `.codex-plugin/plugin.json` 直接属于
正常 plugin。

完成资产同步后重跑 `./install.sh --codex`，再使用新 thread 验证。至少运行：

```bash
python -m pytest -q tests/test_install.py tests/test_codex_assets.py \
  tests/test_codex_doctor.py
goal-plus-codex-doctor --project-root .
```

统一安装器不通过修改 `CODEX_HOME` 为多个 checkout 创建隔离配置。同一 Codex 配置只能注册
一个正常 Goal Plus source；并行 checkout 开发需要在宿主之外提供完整隔离边界，不能让后一次
安装静默替换前一次来源。

## 同时开发 Pi 并使用 Codex

若 Codex 只需正常安装，而 Pi 要跟踪当前 checkout 的 editable 代码，可以一次准备：

```bash
./install.sh --codex --pi-dev
source .goal-plus-dev/set_env.sh
```

Codex 使用所选 Python 环境中的正常 runtime；`pi-goal-plus` 使用自己的开发 venv。两条路径
不会共享 Python executable，也不会通过环境脚本切换宿主账号或认证。

## 改动后的更新动作

| 修改内容 | Pi `--pi-dev` | Codex `--codex` |
| --- | --- | --- |
| `src/goal_plus/**/*.py` | 重启 `pi-goal-plus` | 重跑安装器并新建 thread |
| Python 依赖或 `pyproject.toml` | 重跑安装器并重启 | 重跑安装器并新建 thread |
| `.pi/extensions/**`、`.pi/skills/**`、`package.json` | 重启 `pi-goal-plus` | 无需更新 |
| `.codex/skills/**` | 无需更新 | 同步发布副本、重跑安装器、新建 thread |
| Codex hook 或 plugin 资产 | 无需更新 | 同步相应副本、重跑安装器、重新信任并新建 thread |

Goal Plus 运行状态保存在目标 workspace 的 `.gp/` 中。并行开发即使宿主安装已经隔离，也要
使用不同的目标目录或 Git worktree，避免不同进程写同一份运行状态。

## 相关文档

- [Pi 集成](pi.md)
- [Codex 集成](codex.md)
- [Runtime 状态调试](debugging-runtime.md)
