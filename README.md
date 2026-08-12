# Muyuan

Muyuan 是一个 Agentic Scaling 工程仓库，当前包含两类内容：

- `benchmarks/`：AI 辅助编码、SWE-bench、漏洞挖掘、CANNBench 等评测项目。
- `plugins/`：Goal Plus、投机执行等可独立使用的插件。

```text
muyuan/
├── benchmarks/       # AI 辅助编码、SWE-bench、漏洞挖掘、CANNBench
├── plugins/
└── docs/
```

各 Benchmark 自己管理实验、运行结果和报告；各插件保持独立构建和测试。

详见 [总体架构](docs/architecture.md)。
