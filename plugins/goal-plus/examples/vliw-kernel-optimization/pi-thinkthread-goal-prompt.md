/goal-plus spawn_mode=fresh mode=probe max_parallel=2 max_duration_seconds=900 workspace_provider=thinkthread promotion_mode=artifact_only strategy=agent_guided inner_agent=autoresearch 在 15 分钟内多轮优化 VLIW kernel，并验证 retained candidate 的真实续跑。
<!-- markdownlint-disable-file MD013 MD041 -->

完整采用当前目录的 `thinkthread-search-spec.json`，保留 strategy.global_evidence_mode、worker_budget、
evidence_annotator 和 constraints。source_path 为只读 `workspace-template`，冻结所有
非 solution.py 的评价输入。只允许 candidate 修改自己的 solution.py，不改模拟器、runner、
verifier、案例或 score adapter，不硬编码案例 seed，不访问网络。

只创建一个 K=2 plan、两个长期 candidate，不新增 run、plan 或替换 candidate。首轮每条 lane
只做一个小的、可验证的优化，尽早调用一次 goal_plus_search_run_verifier，收到 accepted 后立即结束
当前 turn。首轮从有界修改开始并提交验证。随后由 Main 使用
goal_plus_session_run 复用同一个 candidate/Child，消费正式 ScoreReport 后继续
下一项小优化；每轮仍只提交一个正式 attempt。即使首次已经降低 cycles，也继续这些 lane，
在时间允许时让每条 lane 至少完成两次正式 verifier 结算。不要用创建新 Child 代替正常续跑。

总预算 15 分钟，最后 4 分钟用于 Annotation、selection、独立 publication verifier、报告和清理。
Main 根据返回的 Evidence 判断是否继续，在 Goal deadline 内等待必要 Annotation 就绪，选择最低
cycles 的有效 exact artifact，完成 artifact_only promotion。不要 apply 回只读模板。

完成 Search result、原始目标审查和 Goal complete，生成 MD/HTML 报告。报告注明 baseline
147734、选中 cycles、各 lane 的实际迭代次数及未完成项。正常流程使用 monitor 和公开工具，
不读取 session-private 内部文件；不要因为首轮有改善就提前关闭候选执行。

模型选择：prompt 不为 main 或任何 subagent 指定模型；所有 subagent（candidate
worker、Evidence annotator 等）默认不指定模型，直接继承 main agent 当前使用的模型。

执行节奏：有方案后及时提交 verifier 验证，切勿长时间内部迭代而不提交方案。
