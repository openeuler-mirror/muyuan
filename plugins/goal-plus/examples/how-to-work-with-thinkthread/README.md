# Goal Plus in a ThinkThread Root

这个例子只验证最小组合：**Pi 运行在 ThinkThread Root 中，Goal Plus 使用 Goal
Mode 完成一个确定性任务**。它不创建 SearchSpec、candidate 或 Git worktree，也不要求
source 是 Git repository。

```text
Host shell
  -> tt run --profile goal-plus-pi --fs <workspace>
     -> protected Pi Root execution
        -> /goal-plus host command
           -> goal_plus_* MCP state machine
              -> main edits greeting.txt and runs verify.py
```

这个切片有意不派生 Child。它先回答一个更基础的问题：不改变 Goal Plus MCP 合同，能否把
同一个普通 Goal Mode 放进 ThinkThread Runtime。Search candidate、private Child、snapshot
verifier 和 `fs.apply` 属于后续的 Pi + ThinkThread Search 切片。

## 1. 前置条件

需要 Linux、可运行的 Pi，以及带内置 `pi` Profile 的 ThinkThread：

```bash
tt --version
tt profile show pi
pi --version
```

从当前 Goal Plus checkout 一次安装 Pi 与 ThinkThread 集成：

```bash
cd /path/to/muyuan/plugins/goal-plus
./install.sh --pi --thinkthread
tt profile show goal-plus-pi
```

安装器不会安装或卸载 ThinkThread RPM。它验证当前 Runtime 和内置 Pi Profile，安装现有 Goal Plus
Python/Pi 组件，再生成受管的 `goal-plus-pi` Profile。该 Profile 沿用 TT 自带 Pi Agent Extension，
显式加载本 checkout 的 Goal Plus extension/skill，只读开放 checkout 与当前 Python environment，
同时开放 Pi 使用的 Node runtime prefix，并把每个 Pi Session 的 Goal Plus 状态放入
Session-private runtime root。已有同名且不归本 checkout 所有的用户 Profile 不会被覆盖。

ThinkThread Runtime、Agent POSIX SDK/CLI 和安装包应来自同一个 Capsule release。当前
Goal Plus 集成方案以 `capsule_public` 的 release manifest 为版本依据；只看到相同的
`tt --version` semver 不能证明 RPM release、protocol fingerprint 和内核保护基线完全匹配。

## 2. 准备一次性工作区

不要直接在 `workspace-template/` 中运行，因为 ThinkThread Root 对 `--fs` 指定的目录使用
direct filesystem。先生成一个可丢弃的工作区：

```bash
cd /path/to/muyuan/plugins/goal-plus
python examples/how-to-work-with-thinkthread/prepare_workspace.py
cd examples/how-to-work-with-thinkthread/workspace
python verify.py
```

最后一条命令此时应失败，因为 `greeting.txt` 仍是待完成状态。

## 3. 在 ThinkThread 中启动 Pi

从工作区启动 Root：

```bash
tt run --profile goal-plus-pi --fs "$PWD"
```

然后把 [pi-goal-prompt.md](pi-goal-prompt.md) 代码块里的单行命令提交给 Pi。这个 prompt
与普通 Goal Plus Goal Mode 相同：triage 后只用一个 `route="main"` 工作项修改文件和运行
验收，不包含 ThinkThread control、Child、Search 或 Git 指令。

另一个 Host 终端可以只读观察：

```bash
tt ps --forest
```

完成后，在工作区确认结果：

```bash
python verify.py
```

Goal Plus record 由 Pi 内的完成卡和本轮 tool Evidence 验收。受管 Profile 不会在 product workspace
生成 Root `.gp`；状态位于该 Pi native Session 的私有目录。这个 no-Search 例子只有 Root，不存在
Root/Child 状态写冲突。

验收条件是：

- `greeting.txt` 精确包含 `Hello from ThinkThread!`；
- `verify.py` 成功；
- Goal Plus record 的 triage 是非优化 Goal Mode；
- work DAG 只有 main-owned work，没有普通 subagent attempt；
- Goal Plus record 不存在 linked Search run。

## 4. 三层接口分别负责什么

| 层 | 这个例子使用的公共接口 | 所有权 |
| --- | --- | --- |
| ThinkThread Host | `tt run`、`tt ps` | 创建、启动、观察 Root；可信 Host 自动化也可使用 Management SDK |
| ThinkThread Runtime | Root direct filesystem 与 protected Pi execution | 承载当前 Pi 进程；本例不派生 Child |
| Goal Plus | `/goal-plus` 加 `goal_plus_record_triage`、`goal_plus_upsert_work_items`、`goal_plus_record_work_event`、`goal_plus_set_status` MCP tools | 目标、work DAG、验收 Evidence 和终态 |

这个最小切片只证明现有两条公共边界可以组合：Host 用 `tt` 创建 protected execution，
Pi 使用现有 Goal Plus MCP/facade 管理任务。它不测试或规定 Pi 对 ThinkThread Agent POSIX API
的可见性，也不把 `tt` 包装成 runtime-owned worker supervisor。

这个例子也不调用 `git-tt` 或 Search candidate pool。Goal Plus Search 的 Child、Agent POSIX、
snapshot Evidence 和 publication 边界见
[ThinkThread 与 Git Workspace 架构](../../docs/workspace-sandbox-arch.md)。

## 5. 停止与重新运行

在 Pi 内正常退出后，Root 会保留为 stopped，可用 `tt ps` 找到完整 ID。确认不再需要它后：

```bash
tt rm <thinkthread-id>
```

已经创建的 ThinkThread 会持久化自己的 Runtime definition。Goal Plus 卸载器不删除 ThinkThread
Runtime、Root 或 state；确认不再需要 Root 后，由 ThinkThread 管理员使用上面的 `tt rm`。下面的
对称命令只移除 Goal Plus 的 Pi 注册和本 checkout 生成的 `goal-plus-pi` Profile：

```bash
cd /path/to/muyuan/plugins/goal-plus
./uninstall.sh --pi --thinkthread
```

重新生成示例工作区时，脚本只会删除自己带有 marker 的目录：

```bash
cd /path/to/muyuan/plugins/goal-plus
python examples/how-to-work-with-thinkthread/prepare_workspace.py --reset
```

不要把工作区 `.gp/`、ThinkThread state、session、control socket、token 或原始 transcript 提交到
Git。普通非 TT Goal Plus 仍使用工作区 `.gp/`；受管 `goal-plus-pi` Profile 使用 Session-private root。
