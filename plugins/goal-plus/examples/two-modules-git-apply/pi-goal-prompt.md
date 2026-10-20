/goal-plus spawn_mode=fresh mode=probe max_parallel=1 max_duration_seconds=600 workspace_provider=git_worktree promotion_mode=apply strategy=agent_guided inner_agent=common 完成双 module Git Search E2E。

目标：必须同时修改 `modules/module_a.py` 和 `modules/module_b.py`，让它们分别定义
`VALUE = 7` 与 `VALUE = 11`。只有两个模块都改对，`python verify.py` 才会返回
`module_score=2` 和 `valid=true`。

工作区合同：读取当前目录的 `search-spec.json` 作为唯一 SearchSpec，冻结 `verify.py`。
调用 `goal_plus_search_freeze_spec` 时，将该 JSON 解析后的对象原样作为 `spec` 传入；不要手工
重建、扁平化、添加或删除字段，也不要把 `workspace`/`strategy` 字段移到顶层。不要调用
`goal_plus_list_models`，宿主已经选定并验证 `gpt-5.6-luna`。
只允许修改 `modules/` 下的两个 module 文件；不要修改 verifier、SearchSpec、marker 或
创建其他候选。先建立一个 K=1 Search，完成一次候选修改并调用
`goal_plus_search_run_verifier`，确认精确分数为 2；随后选择、promotion、apply 到当前 Git root。

验收：apply 后在当前 Git root 再运行 `python verify.py`，输出必须包含
`{"module_a": 7, "module_b": 11, "module_score": 2, "valid": true}`，并确认 `git status`
干净且 HEAD 包含 Goal Plus publication commit。

完成 Goal audit，标记 Goal Plus 为 complete，生成规范 MD/HTML report，并完成 cleanup。
调用 `goal_plus_session_run` 时使用 `timeout_seconds=600`，等待同一 call 完成后再提交 verifier。
