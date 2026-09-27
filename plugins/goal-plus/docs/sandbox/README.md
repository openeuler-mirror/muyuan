# Agent sandbox 设计与验证边界

本文记录 Goal Plus 当前的进程级 sandbox 接线、它能保证的范围，以及还没有通过真实宿主
验证的部分。它不把 Git worktree、`allowed_files` 或 prompt 约束称为操作系统隔离。
完整的 Linux 操作步骤见 [plan.md](plan.md)。

## 结论

当前 checkout 已有一个统一的 `ProcessSandbox` 声明和命令包装入口：Linux 选择
Bubblewrap，macOS 选择 `sandbox-exec`；`native` backend 保留宿主自己的 sandbox 参数。
这解决了“每个 host 各自拼命令”的重复问题，但还没有形成可交付的严格安全边界。

在以下缺口关闭并通过真实 Linux 负向测试以前，不能宣称 candidate 不能改动 workspace
之外的文件：

1. Pi 的结构化/内部推理请求仍有生产构造点没有传入 `ProcessSandbox`。Pi RPC 能被整个
   session 进程包在 Bubblewrap 里，但 RPC 本身不是 sandbox；没有 wrapper 的 RPC 就没有
   操作系统级文件边界。
2. Pi host 为 MCP、session 和 runtime 使用的 `.gp` 路径会进入 writable roots。它们是
   共享运行时目录，不是 candidate 的私有目录；把父进程、MCP 子进程和 candidate 放在
   同一 sandbox 只会扩大可写面。
3. Linux 当前实现先把 `/` 只读绑定，再对 writable roots 做 `--bind`，并且把系统临时
   目录加入 writable roots。实现中随后又创建了 `/tmp` tmpfs；如果把宿主 `/tmp` 重新
   bind 回去，临时目录的隔离语义就被削弱。该组合必须在目标发行版上实测，不得只看
   生成的 argv。
4. Codex 的 `native` backend 不改写 Codex 自己的 `--sandbox` 配置，也不证明它会阻止
   native agent 通过其可用工具写入外部路径。这里需要 Codex 宿主的实际行为证据。
5. wrapper 目前不提供完整的进程、IPC、mount、credential 或读隔离承诺；目标首先是
   “workspace-write 时只允许明确的写根，read-only 时不允许写入”，其他能力要单独测量。

因此，现阶段的正确描述是：**有统一的启动策略原型，尚未完成 strict no-outside-write
验收**。如果负向测试失败，必须保留失败证据并修复接线或策略，不能把失败改写成测试环境
不支持后继续报告成功。

## 当前调用链

```text
candidate/session input
        |
        v
ProcessSandbox(mode, backend, workspace, writable_roots, network)
        |
        v
resolve_sandbox_command(...)
        |
        +-- linux  -> bwrap --ro-bind / / ... --bind <writable-root> ...
        +-- macOS  -> sandbox-exec -p <seatbelt profile> ...
        +-- native -> 原命令（由 Codex/Pi 自己执行 sandbox）
```

主要实现入口：

- [ports/sandbox.py](../../src/goal_plus/ports/sandbox.py)：声明 mode、backend、workspace 和
  writable roots。
- [execution/sandbox.py](../../src/goal_plus/execution/sandbox.py)：解析路径并生成 Bubblewrap
  或 Seatbelt 命令。
- [execution/session_rpc.py](../../src/goal_plus/execution/session_rpc.py)：在进程注册 gate
  前应用 wrapper，之后再建立 RPC 连接。
- [infrastructure/native_session_inputs.py](../../src/goal_plus/infrastructure/native_session_inputs.py)：
  组装 Codex/Pi session 的 sandbox 声明和 runtime 路径。
- [assets/pi/extensions/goal-plus.ts](../../assets/pi/extensions/goal-plus.ts)：报告 Pi
  选择的外层 sandbox 后端；这只是 host 观测，不是内核证明。

## 角色和当前状态

| 路径 | 当前声明 | 当前判断 |
| --- | --- | --- |
| Pi candidate / ordinary worker | outer sandbox，workspace-write | 需要验证；共享 `.gp` roots 和 callback 进程仍是阻断项 |
| Pi final check / read-only worker | outer sandbox，read-only | 需要验证，特别是写入拒绝和 RPC 续跑 |
| Codex native candidate | Codex native sandbox 参数 | 依赖 Codex 实际行为；Goal Plus wrapper 不会把 native 变成 outer |
| Structured inference / LLM verifier | 请求可选 sandbox，但部分调用点未传入 | 当前不能作为完整隔离证据 |
| Git worktree / `allowed_files` | 应用层 artifact/edit 边界 | 不是 OS sandbox，不能拦截任意 shell、解释器或子进程写入 |

## 安装与依赖

`install.sh` 是唯一支持的源码安装入口，但当前没有 `--sandbox` 参数，也不会替 Linux
发行版安装 Bubblewrap。Linux 主机必须先用系统包管理器安装 `bwrap`，再按
[plan.md](plan.md) 做 preflight；缺少后端时应该 fail closed，而不是退回裸命令。

常规安装仍使用：

```bash
./install.sh --pi       # 或 ./install.sh --codex
./install.sh --runtime-info
```

现场验证要记录 `git rev-parse HEAD`、安装 build、`bwrap --version` 和宿主实际解析的
launcher。开发态 Pi 使用 `./install.sh --pi-dev` 与 `.goal-plus-dev/set_env.sh`，不要把
开发 checkout 当成 release 安装证据。

## 证据规则

单元测试只能证明 argv 形状、缺少后端时的错误和声明传递，不能证明内核真的拒绝写入。
真实验收至少需要：

- allowed workspace 的写入成功；
- peer workspace、checkout 外的专用目录和绝对路径写入失败；
- symlink、rename、hardlink、子进程和解释器写入没有绕过规则；
- read-only worker 的写入失败；
- 后端缺失时没有裸命令 fallback；
- RPC 建立、续跑、超时、关闭和 cleanup 均发生在同一隔离策略下。

每次现场运行应保存命令、发行版/内核、宿主版本、sandbox 后端版本、返回码和简短日志。
不要把 token、认证环境变量、完整模型输出或包含秘密的 `.gp` 目录提交到仓库。
