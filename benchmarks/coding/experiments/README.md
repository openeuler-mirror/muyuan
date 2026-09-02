# Experiments

实验配置在 [`../configs/runs/`](../configs/runs/)。本地运行产物写到模块根下的 `runs/`（gitignore，不入库）。

对照说明与需要提交的 case 列表按实验放在本目录。

**需要长期留存的运行证据也放这里**，按实验一个目录，并且是从 `runs/` 里**挑选并脱敏**后的副本 ——
不是整份 `runs/<id>/`。原因见 [`pi-glm52-clean2026-20260825/README.md`](pi-glm52-clean2026-20260825/README.md)：
`runs/` 含缓存、工作区副本、网关地址与本机路径，其中前两类没有留存价值，后两类不能入库。

| 实验 | 内容 |
|---|---|
| [`pi-glm52-clean2026-20260825/`](pi-glm52-clean2026-20260825/) | pi 0.84.3 + GLM-5.2 在 `_clean2026` 上的三轮重复（+1 旁证）：manifest、summary、逐例结果、逐步工具调用、模型补丁、harness 自动生成的每轮报告 |
