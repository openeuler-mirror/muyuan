# Goal Plus 开发指南

Goal Plus 由共享 Python runtime、Pi package 和 Codex plugin 组成。仓库只提供一个统一
安装入口：`install.sh`。Codex 使用正常安装；Pi 另外提供 checkout-local 开发态，便于在不
切换 Pi 账号配置的情况下测试当前源码。

修改防御性 runtime 规则前先阅读仓库级 [开发约定](../AGENTS.md) 和
[系统规格](../SPEC.md)。本页只说明安装、刷新和本地验证环境，不重复运行时行为合同。

## 安装模式

| 命令 | Python runtime | 宿主资产 | 宿主配置 |
| --- | --- | --- | --- |
| `./install.sh --codex` | 非 editable，受管 Python 3.12 独立 venv | 注册已验证的不可变 Codex plugin 副本 | 写正常 Codex marketplace/plugin 注册 |
| `./install.sh --pi` | 非 editable，受管 Python 3.12 独立 venv | 注册已验证的不可变 Pi package 副本 | 写 Pi 用户 package 注册 |
| `./install.sh --pi-dev` | editable，安装到 `.goal-plus-dev/venv` | launcher 显式加载当前 Pi extension 和 skill | 不修改 Pi 用户配置 |

`--pi` 和 `--pi-dev` 互斥；`--codex --pi` 与 `--codex --pi-dev` 都有效。
安装入口通过 uv 自动取得受管 Python 3.12；没有 uv 时将固定版本的 uv 安装到专属
bootstrap 目录。无需激活 Conda/venv，`PYTHON` 不再选择安装目标。

```bash
./install.sh --codex --pi
./install.sh --runtime-info
```

正常安装保存在 `${GOAL_PLUS_INSTALL_HOME:-~/.local/share/goal-plus}/releases/<build>/`：
`package/` 保存源码及宿主资产快照，`venv/` 使用提交的 `uv.lock` 安装依赖。
代码、资产、锁文件或来源路径变化会生成新的 build；固定的项目版本号不能代替 build。
Codex 缓存版本带有 build，安装后的 hook/MCP 使用 release 内 launcher 的绝对路径，
因此已加载的启动命令不依赖会被 Codex 刷新清理的旧缓存；Pi extension 从自身包目录读取
安装收据。两端都使用绝对 Python 路径、隔离模式及预期 build 校验，正常运行不从 `PATH`、
`PYTHONPATH`、`GOAL_PLUS_SOURCE_PATH` 或目标 workspace 寻找 Goal Plus。
内部 Python 进程沿用该解释器与受控模块路径。只有 `pi-goal-plus` 显式开发入口加载 editable 源码。

本地和 ThinkThread verifier 中的裸 `python` / `python3` 也绑定运行中的 Goal Plus
解释器；不读取 `GOAL_PLUS_PYTHON` 或宿主 PATH 来重新选择。需要其他 Python 依赖环境的
verifier 应显式填写解释器路径，ThinkThread 下还需由管理员授权该环境的只读路径。
Verifier 保留自身工作目录的模块导入，但不继承外部 `PYTHONHOME`、`PYTHONPATH` 或用户
site-packages。`--pi-dev` 的导入检查、冲突检查和工具调用同样使用独立 venv 的隔离模式。

产品资产源码只在 `assets/codex/` 和 `assets/pi/` 维护。源码 checkout 不保留产品的
`.codex/skills`、`.codex/agents`、`.pi/extensions`、`.pi/skills`、`.pi/prompts` 或根级插件声明。
`AGENTS.md` 继续指导开发，`install.sh`、`uninstall.sh` 是显式执行入口。
Codex marketplace 源文件是 [assets/codex/marketplace.json](../assets/codex/marketplace.json)。
安装器只在 release 内生成 `.agents/plugins/marketplace.json`；不要在源码 checkout 恢复同名发现入口，
否则 Codex 从该目录发现插件时可能将已安装的 release 缓存替换为未准备的源码。
`codex plugin add` 后，安装器继续核验 inventory 中的启用状态、来源和版本，以及实际缓存中的
manifest、runtime 收据、解释器/build 标记、MCP 和 hooks；校验失败恢复已有来源，首次安装失败
移除本次新增注册，保留已准备的 release。缓存仍被其他旧 checkout 或旧 Codex 进程刷新时，
先结束相关进程并更新那些 checkout，再重跑安装器；安装器不删除任意旧 checkout。

Pi 的 release `package.json` 从 `assets/pi/package.json` 生成，显式引用 `assets/pi/` 中的资源。
安装后使用 Pi 自身的资源解析器检查实际启用的 extension、skill 和唯一 package 来源，再核验
Python build。缺失、禁用、重复或指向旧资源时安装失败并恢复原 settings；不调用模型。
正常安装需要 npm 安装的 Pi CLI 及 Node.js，以便使用相同版本的资源解析器。

已安装所选宿主时，安装器列出来源并询问 `Uninstall and reinstall ...? [y/N]`。
输入 `y` 才继续，`n`、空输入或 EOF 取消且不修改注册。自动化可用 `--yes`：

```bash
./install.sh --codex --pi --yes
```

确认后先准备并验证新 release，再卸载重装所选宿主注册；即使 build 相同也会重新注册。
保留旧 release 和其他宿主引用，Pi 更新保留 package 过滤设置；过滤导致所需资源未启用时
报告校验失败，不能静默启用。`--dry-run` 不询问、不替换注册。

准备及身份验证成功后才修改宿主注册；更新已有宿主注册失败时恢复原来源。
Codex、Pi 和 ThinkThread Profile 分别持有版本引用，更新其中一个不会更新另一个。
因此 `./install.sh --codex --pi` 不刷新已有 `goal-plus-pi` TT Profile；还使用 TT 时执行
`./install.sh --pi --thinkthread`。同样，`./uninstall.sh --pi` 只移除普通 Pi 注册；
要移除本 checkout 管理的 TT Profile，显式加 `--thinkthread`。无参数卸载默认选择全部组件。
安装不会删除旧 release，旧会话可继续使用原版本；正常安装成功后源码 checkout 可以移动，
后续从新位置安装可在提示中确认替换旧来源注册。可用 `GOAL_PLUS_UV` 指定 uv 的绝对路径。
首次安装需要下载 Python 和依赖；`--dry-run` 不写 release 或宿主注册，但 shell 入口可能先准备
执行检查所需的 uv/Python。

安装器会先检查同名宿主资源。旧版 staging 安装或散落的同名 Pi asset 会使安装失败并显示
冲突来源；普通 package 的来源不同可通过重装提示确认替换。Pi 的显式 `extensions`、`skills`
配置也会被识别；卸载可以移除明确的 Goal Plus 资源注册，资源文件本身保留。

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
assets/pi/extensions/goal-plus.ts
assets/pi/skills/goal-plus/SKILL.md
```

当前 Pi package 只有这一个面向用户的 skill。Search 是 Goal Plus 内部模式，不是第二个
用户级 Pi skill。

### 从目标 workspace 启动

```bash
cd /absolute/path/to/target-workspace
pi-goal-plus
```

用户级或目标项目若已显式引用相同 `assets/pi/` extension/skill，Pi 会按绝对资源路径去重。
旧式直接注册源码根目录的 package 已不再有效，需先移除该注册。
安装时和 launcher 每次启动时仍会拒绝来自其他 checkout 或复制路径的 Goal Plus；若
报错，先按报告的 source 执行 `pi remove <source>`。断链的旧式 loose-resource symlink 不会被
Pi 加载，也不会阻塞开发态。其他 Pi package、模型设置和认证信息不会被修改。

### Pi 开发循环

1. 修改 `src/goal_plus/`、`assets/pi/extensions/`、`assets/pi/skills/` 或 `assets/pi/package.json`；
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

这会准备新的受管 runtime，并保留唯一的正常 plugin ID `goal-plus@goal-plus`。每次安装都会
通过 `codex plugin add` 刷新 plugin 缓存，即使该插件已安装；不会仅因来源相同而跳过
hooks/skills 更新。安装或代码更新后
新建 Codex thread；hook 内容变化时，在 `/hooks` 中重新检查并信任，再新建一个 thread。

若旧开发安装仍存在，先显式删除注册：

```bash
codex plugin remove goal-plus@goal-plus-local
codex plugin marketplace remove goal-plus-local
```

确认 `${CODEX_HOME:-~/.codex}/local-marketplaces/goal-plus` 确实是已退休 installer 生成的目录
后，才由开发者自行删除。统一安装器不会递归删除用户目录。

### 修改 Codex 资产

`assets/codex/skills/` 是唯一 skill 源码；`hooks.json`、`mcp.json` 和 `plugin.json` 是发布模板。
安装器在 release 中生成 `skills/`、`hooks/hooks.json`、`.mcp.json` 和 `.codex-plugin/plugin.json`。
不在源码生成同步副本，也不建立回指资产目录的自动发现 symlink。

修改后重跑 `./install.sh --codex`，再使用新 thread 验证。至少运行：

```bash
python -m pytest -q tests/test_install.py tests/test_codex_assets.py \
  tests/test_codex_doctor.py
./install.sh --runtime-info
```

统一安装器不通过修改 `CODEX_HOME` 为多个 checkout 创建隔离配置。同一 Codex 配置只能注册
一个正常 Goal Plus source；并行 checkout 开发需要在宿主之外提供完整隔离边界，不能让后一次
安装静默替换前一次来源。

切换正常 Codex 或 Pi 安装来源时，可以直接在新 checkout 运行 `./uninstall.sh --codex`
或 `./uninstall.sh --pi`，再运行对应安装命令。卸载器按宿主注册识别 Goal Plus，来源不同只
打印警告并继续移除，不询问确认；不需要回到旧 checkout。`--dry-run` 只展示计划。
`--pi-dev`、`--thinkthread` 和 `--runtime` 仍使用各自的归属检查。
宿主卸载仅移除对应注册与版本引用。结束旧会话后，使用 `./uninstall.sh --runtime` 清理
不再被任何已登记宿主引用的 release；仍存在未选择的宿主注册时拒绝清理。
旧的 pip/Conda 安装不会自动删除，也不再被正常宿主入口使用。bootstrap 的 uv/Python 保留供
后续安装使用，目标 workspace 的 `.gp/` 不受影响。

## 同时开发 Pi 并使用 Codex

若 Codex 只需正常安装，而 Pi 要跟踪当前 checkout 的 editable 代码，可以一次准备：

```bash
./install.sh --codex --pi-dev
source .goal-plus-dev/set_env.sh
```

Codex 使用受管的不可变 runtime；`pi-goal-plus` 使用自己的开发 venv。两条路径
不会共享 Python executable，也不会通过环境脚本切换宿主账号或认证。

## 改动后的更新动作

| 修改内容 | Pi `--pi-dev` | Codex `--codex` |
| --- | --- | --- |
| `src/goal_plus/**/*.py` | 重启 `pi-goal-plus` | 重跑安装器并新建 thread |
| Python 依赖或 `pyproject.toml` | 重跑安装器并重启 | 重跑安装器并新建 thread |
| `assets/pi/**` | 重启 `pi-goal-plus` | 无需更新 |
| `assets/codex/skills/**` | 无需更新 | 重跑安装器、新建 thread |
| Codex hook 或 plugin 资产 | 无需更新 | 重跑安装器、重新信任并新建 thread |

Goal Plus 运行状态保存在目标 workspace 的 `.gp/` 中。并行开发即使宿主安装已经隔离，也要
使用不同的目标目录或 Git worktree，避免不同进程写同一份运行状态。

## 相关文档

- [Pi 集成](pi.md)
- [Codex 集成](codex.md)
- [Runtime 状态调试](debugging-runtime.md)
