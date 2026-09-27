# Linux sandbox 现场验证计划

这是一份可执行的 Linux 验证清单，目标是证明 **subagent 在 workspace-write 模式不能
修改 workspace 之外的文件**，并找出 RPC、MCP、续跑和 cleanup 的绕过路径。计划完成前，
不要把 Goal Plus 描述成已经提供严格 sandbox。用户要求保留这份 plan 作为真实 Linux
跑通流程的交接记录；验收完成后应把结果绑定到提交或评审记录，并删除过时的施工内容。

## 0. 通过标准和停止条件

一次运行只有同时满足以下条件才算通过：

- sandbox 后端缺失、权限不足或 namespace 不可用时，启动明确失败，不执行裸命令；
- workspace 内允许的文件能创建、修改和删除；
- peer workspace、checkout 外专用目录、绝对路径和通过 symlink/rename/hardlink 指向的
  外部目标均写入失败；
- read-only 角色写入失败；
- 子进程、shell、Python/Node 等解释器继承同一限制；
- Pi RPC 的 spawn、message、resume、interrupt、destroy 和 final-check 都使用同一策略；
- session gate、进程登记、EOF、timeout 和 cleanup 不会让 wrapper 被绕过。

出现以下任一结果立即标记 **FAIL**，先保存日志再停止 E2E：外部文件内容改变、wrapper
缺失却继续运行、声明的 writable root 与实际路径不一致、RPC 子进程脱离 wrapper、或
timeout 后仍有可写的孤儿进程。不要用“模型没有主动尝试”替代负向测试。

## 1. 主机和 checkout 记录

在 Linux disposable VM 或专用机器中执行。不要在含有凭据的真实 home 上测试；所有测试
目录放到专用目录，并在结束时删除。

```bash
set -eu
export GP_SANDBOX_RUN="$HOME/.cache/goal-plus-sandbox-validation/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$GP_SANDBOX_RUN"
cd /path/to/muyuan/plugins/goal-plus
git rev-parse HEAD | tee "$GP_SANDBOX_RUN/goal-plus-head.txt"
uname -a | tee "$GP_SANDBOX_RUN/uname.txt"
cat /etc/os-release | tee "$GP_SANDBOX_RUN/os-release.txt"
python3 --version | tee "$GP_SANDBOX_RUN/python-version.txt"
bwrap --version | tee "$GP_SANDBOX_RUN/bwrap-version.txt"
```

记录 `id`、`mount`、`unshare --user --map-root-user true` 是否可用，以及发行版是否关闭
unprivileged user namespaces。不要为了测试临时修改生产机器的全局 sysctl；如果 namespace
被策略禁止，记录为 **环境阻断**，换 disposable VM 重跑。

## 2. 安装和后端 preflight

安装器不负责安装 Bubblewrap。先按发行版文档安装 `bwrap`，再运行仓库的支持入口：

```bash
./install.sh --pi --yes
./install.sh --runtime-info
```

如果测试 Pi checkout-local 代码，改用：

```bash
./install.sh --pi-dev
source .goal-plus-dev/set_env.sh
```

记录 `runtime-info` 中的 build 和实际 Pi launcher。确认没有从另一个 checkout、旧 package
或用户级自动发现入口加载 Goal Plus。先执行现有安装测试：

```bash
uv run --frozen python -m pytest --run-system-tests -n 0 -q tests/test_installation_system.py
```

安装测试通过不等于 sandbox 通过；它只证明安装身份和宿主注册。

## 3. 直接 wrapper 负向测试

先绕开模型，使用一个只做文件操作的 Python 子进程验证内核边界。workspace、runtime、
peer 和 outside 必须是同一专用 run 目录下的不同子目录，且不要放在 `/tmp`，以免当前
实现自动加入全局临时目录造成假阳性。

用 `resolve_sandbox_command` 生成命令并记录完整 argv，然后逐项运行以下操作：

| 操作 | workspace-write 预期 | read-only 预期 |
| --- | --- | --- |
| 创建/修改 workspace 文件 | 成功 | 失败 |
| 修改 peer 和 outside 文件 | 失败 | 失败 |
| shell、Python、Node 子进程写文件 | 失败（外部） | 失败 |
| symlink 指向 outside 后写入 | 失败 | 失败 |
| rename/hardlink 到 outside | 失败 | 失败 |
| `cwd` 在 workspace 外 | 启动失败或按显式 root 规则拒绝 | 启动失败或拒绝 |

建议每次操作都使用一个全新目标文件，并在 wrapper 返回后比较 SHA-256 和文件是否存在：

```bash
uv run --frozen python - <<'PY'
from pathlib import Path
import hashlib, os, subprocess, sys
from goal_plus.execution.sandbox import resolve_sandbox_command
from goal_plus.ports.sandbox import ProcessSandbox

root = Path(os.environ["GP_SANDBOX_RUN"]).resolve()
workspace = root / "workspace"
runtime = root / "runtime"
peer = root / "peer"
outside = root / "outside"
for path in (workspace, runtime, peer, outside):
    path.mkdir(parents=True, exist_ok=True)

target = outside / "must-not-change.txt"
target.write_text("before\n")
request = ProcessSandbox(
    mode="workspace-write", backend="outer", workspace=workspace,
    writable_roots=(runtime,), network="deny",
)
command = resolve_sandbox_command(
    [sys.executable, "-c", f"from pathlib import Path; Path({str(target)!r}).write_text('after\\n')"],
    cwd=workspace, sandbox=request,
)
print("resolved:", command)
result = subprocess.run(command, cwd=workspace, text=True, capture_output=True)
print("returncode:", result.returncode)
print("stdout:", result.stdout)
print("stderr:", result.stderr)
assert target.read_text() == "before\n", "FAIL: outside file changed"
PY
```

这个最小脚本若失败，先不要继续跑模型 E2E。补充测试必须覆盖 runtime/session root 是否
真的需要写权限，以及它们是否意外包含 peer workspace、Goal 状态或其他 candidate 的
artifact。若必须让 MCP 写入 `.gp`，请把 host bridge 和 candidate execution 分成不同的
安全域后再验收；把整个 `.gp` 加入 writable roots 不能算隔离。

## 4. 真实宿主和 RPC 生命周期

完成直接 wrapper 负向测试后，按顺序运行，不要并发多个真实宿主：

```bash
uv run --frozen python -m pytest --run-system-tests -n 0 -v -s -rs \
  tests/st/test_st_native_session.py \
  tests/st/test_st_session_host.py \
  tests/st/test_st_session_control.py
```

再按宿主分别运行 Pi 和 Codex 的 native/session 场景。使用测试文档规定的
`ST_PI_MODEL`、`ST_CODEX_MODEL`、`ST_PI_BINARY` 等环境变量；模型名和凭据不要写入本文件
或提交的日志。运行前用 `--collect-only` 确认精确 node id，避免误跑高成本场景。

重点观察：

1. Pi candidate 的 MCP callback 是否由 sandbox 内进程执行，是否能创建外部 probe 文件；
2. Pi 的 resume/interrupt/destroy 后是否仍有可写子进程；
3. Codex native sandbox 的实际写入边界是否与声明一致；
4. final-check/read-only 是否拒绝写入；
5. `GOAL_PLUS_ST_TMP`、`.gp/host-sessions`、runtime artifact 与 peer workspace 的路径
   是否被误放进同一个 writable root。

必要时先查看已有诊断入口和参数：

```bash
scripts/monitor_goal_plus.sh --help
python3 scripts/check_session_instructions.py --help
```

按 [Runtime 调试](../debugging-runtime.md) 为实际 session 提供 `history`、`--agent-harness`
和对应 build 的 `--overview`；只复用脚本支持的参数。把生成的监控摘要放在
`$GP_SANDBOX_RUN`，不要改写仓库状态目录。

## 5. Goal Plus 端到端流程

确认直接 probe 和 session lifecycle 均通过后，再运行一个最小 Search/Goal 场景，验证业务
状态与隔离证据同时成立：

```bash
uv run --frozen python -m pytest --run-system-tests -n 0 -v -s -rs \
  tests/st_pi/test_goal_plus_pi.py -k 'circle_packing_git_common_agent_apply'
```

若使用 Codex，选择 `tests/st` 中对应的 unified-main 场景，并先执行：

```bash
uv run --frozen python -m pytest --run-system-tests -n 0 \
  tests/st/test_st_unified_main.py --collect-only -q
```

验收内容：candidate 能在自己的 workspace 产生预期 artifact；peer/outside probe 未变化；
verifier、selection、promotion 和 cleanup 的状态正常；报告中明确记录 sandbox backend、
workspace、writable roots 和任何失败尝试。功能成功但越界 probe 成功仍然是 **FAIL**。

## 6. 证据包和结果格式

在 `$GP_SANDBOX_RUN/result.md` 记录：

```text
build / git head:
host / distro / kernel:
bwrap version / namespace preflight:
launcher and install mode:
Pi/Codex model (name only, no credentials):
workspace / runtime / peer / outside paths:
direct wrapper result:
RPC spawn/resume/interrupt/destroy result:
read-only result:
E2E result:
first failing command and stderr:
remaining known gaps:
```

把 probe 的 stdout/stderr、返回码、文件 hash、scope/session 摘要一并保存。只提交不含秘密
的摘要，并把完整证据绑定到 commit 或评审记录；不要提交 `$GP_SANDBOX_RUN`、模型 transcript
或用户 home 的复制品。

## 7. macOS 后续验证

Linux 通过后，在 macOS 重复同一套 direct probe 和 Pi/Codex lifecycle。特别记录 Seatbelt
profile、`sandbox-exec` 可用性、`/private/tmp` 与 `TMPDIR` 的实际路径，以及 symlink/rename
结果。macOS 的 `sandbox-exec` 与 Linux Bubblewrap 不是同一 policy language，Linux 通过不
能自动推导 macOS 通过。
