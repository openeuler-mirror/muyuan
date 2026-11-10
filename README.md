# Muyuan

Muyuan 是一个 Agentic Scaling 工程仓库，当前包含两类内容：

- `benchmarks/`：AI 辅助编码、SWE-bench、漏洞挖掘、CANNBench 等评测项目。
- `plugins/`：可独立使用的插件与推理组件，包括 Goal Plus、Inference Scaling、Self Speculation 和 Pi Speculative Action。

Goal Plus 入口：[中文说明](plugins/goal-plus/README_zh.md) ·
[English](plugins/goal-plus/README.md) · [文档地图](plugins/goal-plus/docs/README.md) ·
[运行示例](plugins/goal-plus/examples/README.md)。

推理与投机执行项目：[Inference Scaling](plugins/inference_scaling/README.md) ·
[Self Speculation](plugins/self-speculation/README.md) ·
[Pi Speculative Action](plugins/pi-speculative-action/README.md)。

```text
muyuan/
├── benchmarks/       # AI 辅助编码、SWE-bench、漏洞挖掘、CANNBench
├── plugins/
└── docs/
```

各 Benchmark 自己管理实验、运行结果和报告；各插件保持独立构建和测试。

详见 [总体架构](docs/architecture.md)。
