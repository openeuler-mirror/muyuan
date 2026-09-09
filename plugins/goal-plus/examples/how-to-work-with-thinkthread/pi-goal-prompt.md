# Pi Goal Prompt

```text
/goal-plus max_duration_seconds=300 在当前工作区完成一个确定性的普通 Goal Mode 任务：将 `greeting.txt` 的内容精确改为 `Hello from ThinkThread!` 并保留末尾换行，然后运行 `python verify.py`。这个任务不是优化问题，不需要多假设探索；triage 后只建立一个 `route="main"` 工作项，不派发 subagent，不创建 SearchSpec，不调用任何 `search_*` 工具。验收通过后记录 work item accepted，并以实际命令输出为 Evidence 将 Goal Plus 标记为 complete。
```
