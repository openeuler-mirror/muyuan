# Candidate Dispatch：fresh 与 fork

Candidate 的 `fresh` 和 `fork` 是同一个派发生命周期的两种初始上下文来源。它们都创建
相同的 candidate session、target identity、workspace binding、provider receipt 和
continuation；差异只在 provider operation 之前，是否从一个已完成的父 checkpoint 派生。

## 统一生命周期

```text
 Main / Search
      |
      | 1. prepare intent
      |    run + candidate + budget + workspace + request identity
      v
 persist session identity and dispatch target(s)
      |
      +-----------------------------+
      |                             |
      | session_mode="fresh"        | session_mode="fork"
      |                             |
      | no parent checkpoint        | require completed parent checkpoint
      | create from frozen baseline  | derive isolated branch/private Fs
      |                             |
      +--------------+--------------+
                     v
             provider operation
       direct: start / fork native session
       ThinkThread: create / fork Child and workspace
                     |
                     v
             persist exact receipt
       provider session + request + checkpoint/parent
       workspace/Fs attachment + cleanup facts
                     |
                     v
              bind native session
       verify run/candidate/generation/identity
                     |
                     v
             open -> wake -> run
       same goal_plus_session_run continuation path
                     |
                     v
       verifier settlement -> close -> selection
       -> promotion/apply -> report and cleanup
```

`fresh` 与 `fork` 不应分别拥有一套 session 状态机。内部 coordinator 使用
`start_agent_session(..., session_mode="fresh"|"fork")`，并把模式特有的 provider 操作
限制在 `ProviderDispatchAdapter` 内。`prepare_fork()` 已删除；保留的
`goal_plus_search_fork` 只是 Main 使用的 façade，仍进入这条统一路径。

## 两种模式只差在哪里

| 维度 | fresh | fork |
| --- | --- | --- |
| 初始来源 | FrozenSpec 的 baseline、candidate workspace 或 provider S0 | 父 Main 的完整 completed checkpoint |
| provider 操作 | 创建新的 native session/Child | 从父 session/Child 派生 native branch/Child 和隔离 workspace/Fs |
| 父身份 | 不需要 parent checkpoint | 必须保存并校验 parent session、checkpoint 和 boundary |
| 首次 continuation | 打开新 session 后发送 candidate launch | 打开派生 session 后发送同一 candidate launch；历史由 provider 保留 |
| 后续 continuation | 复用当前 session 或按合同创建下一 generation | 复用原派生 binding；不能用 fresh spawn 冒充丢失的 fork provenance |
| Search 入口 | `goal_plus_search_start_batch` | `goal_plus_search_fork` façade |
| 共同入口 | `start_agent_session(..., session_mode="fresh")` | `start_agent_session(..., session_mode="fork")` |

`goal_plus_session_run`、`wait`、`message`、`interrupt`、`stop`、`close`、verifier
settlement、selection、promotion 和 report 对两种模式相同。模式不会改变 candidate
context、预算、Evidence 或 publication 的业务合同。

`ForkPoint` 若出现在 replay 子系统中，仍表示独立的历史 replay 选择点；它不是 candidate
dispatch 的 `fork` session mode，也不应与 `DispatchGroupState` 混用。

## 持久状态与重启

派发状态由 coordinator 持久化，provider receipt 是恢复依据：

```text
 prepared
    | provider fence not crossed
    | restart may retry provider startup exactly once
    v
 submitted -------- provider outcome unavailable --------> unknown
    |                                                     |
    | exact receipt recovery                              +--> fenced
    v                                                          recover or fail closed
 started
    |
    | receipt complete; finalize/bind/close is idempotent
    v
 closed / failed / cleaned
```

- 整个 group 仍是 `prepared` 且没有 target 提交记录时，重启先恢复 provider startup。
- 任一 target 已进入 `submitted` 或 `unknown` 后，只能恢复原 request 的 receipt/journal，
  不能盲目重新派发。
- exact receipt 已全部保存但 bind/finalize 尚未完成时，只做 bind/finalize，不再次调用
  provider。
- 未知 outcome 保留 session、target 和 request identity 的 fence；缺少可审计 receipt 时
  fail closed。
- launch receipt 不可变，close/cleanup facts 独立记录；provider 关闭事实不改变已保存的
  launch provenance。

这些规则同时适用于 fresh provider start 和 fork provider derivation。fork 额外需要验证
parent checkpoint；fresh 额外需要验证 baseline/workspace。两者都必须在 provider 调用前
持久化 target identity，并在 provider receipt 写入后才绑定 native session。

## façade 与 owner

```text
MCP façade                    shared owner                 provider adapter
---------------------------   --------------------------   -------------------------
search_start_batch            candidate session coordinator direct / ThinkThread start
search_fork                   candidate session coordinator direct / ThinkThread fork
session_run / wait / close    session control + host       native open/wake/message/close
search_run_verifier           settlement owner             frozen verifier execution
select / promote / apply      Search/publication owners    artifact/publication I/O
```

`goal_plus_search_fork` 保持原名称和 caller authorization，避免新增第二个 fork 工具。
它返回统一 dispatch receipt；`search_start_agent_session` 继续服务单 candidate fresh
session。业务状态转换、幂等和恢复决策属于 shared coordinator；adapter 只返回 checkpoint、
provider journal、native receipt、workspace attachment 和 cleanup facts。

## 审查时的最小检查

修改 candidate 派发时，应同时检查以下共同事实，而不是只检查某个 provider 的调用顺序：

1. fresh/fork 是否都经过同一个 session identity、target persistence 和 continuation。
2. provider 调用前是否已保存所有 target，receipt 是否包含准确 request identity。
3. prepared、submitted、unknown、started、closed 的恢复是否遵守同一 fence 规则。
4. fork 是否校验 completed parent checkpoint，fresh 是否校验 baseline/workspace。
5. session control、settlement、selection、promotion、report 和 cleanup 是否继续复用原 owner。

宿主差异、Codex/Pi/ThinkThread 的具体能力和支持矩阵见
[Agent Host Adapters](agent-host-adapters.md)。Search 的 checkpoint recovery 与 candidate
dispatch recovery 的边界见 [Search 一致性恢复](search-recovery.md)。工具参数和返回字段见
[API](api.md)。
