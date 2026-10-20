/goal-plus spawn_mode=fresh max_parallel=2 workspace_provider=thinkthread promotion_mode=apply strategy=agent_guided inner_agent=autoresearch 优化 `workspace/initial_program.py` 的 Circle Packing 解。

问题：当前程序需要在单位正方形内放置恰好 26 个互不重叠的圆，并尽可能增大
所有圆的半径之和。

修改：只修改 `workspace/initial_program.py`。不要修改 verifier 或其他任务输入，
不要访问网络。实现只使用 Python 标准库，禁止 numpy、scipy 等第三方依赖
（执行环境未安装，verifier 会直接失败）。

工作区合同：`workspace/` 是本任务的 ThinkThread 托管工作区，由 AutoResearch
inner agent 的 Child-private Git 维护 attempt 历史和物理 `results.tsv`。把当前目录中的
`search-spec.json` 作为权威 SearchSpec，保持 `source_path="."` 和
`inner_agent="autoresearch"`；worker 只读检查这些历史，不得自行 commit、reset、
rebase、改写 refs 或编辑 `results.tsv`。不要把当前示例目录或其外层仓库当作 source。

Search 要求：这是强制进入 Search Mode 的任务。

验收：运行 `python workspace/.goal-plus-verifiers/circle_packing_score.py`。结果必须
满足 `valid=true`，圆的数量必须为 26，且 `sum_radii >= 2.2` 即视为达标；本示例用于
验证流程，达标后正常收尾即可，不要求继续提高分数。
选中结果必须通过 provider publication 回写到 `workspace/`；回写后从源码工作区
重新运行 verifier，并确认结果一致。两个候选通道都至少产出一个
verifier 已结算结果后结束探索。

模型选择：prompt 不为 main 或任何 subagent 指定模型；所有 subagent（candidate
worker、Evidence annotator 等）默认不指定模型，直接继承 main agent 当前使用的模型。

执行节奏：有方案后及时提交 verifier 验证，切勿长时间内部迭代而不提交方案。
