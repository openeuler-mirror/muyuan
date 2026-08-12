# Muyuan 目录设计

```text
muyuan/
├── benchmarks/
│   ├── coding/
│   ├── swebench/
│   ├── vulnerability/
│   ├── cannbench/
│   └── templates/
├── plugins/
│   ├── goal-plus/
│   └── speculative-execution/
├── docs/
└── README.md
```

## Benchmarks

每个 Benchmark 自己管理：

- 测试集与执行脚本；
- 测试环境和判分方法；
- 实验配置与运行结果；
- 报告模板和生成脚本。

```text
benchmarks/<name>/
├── src/
├── tests/
├── configs/
├── scripts/
├── experiments/
└── reports/
```

## Plugins

Goal Plus 和投机执行放在 `plugins/` 下。每个插件保留自己的源码、配置、测试、文档和构建入口，可以脱离 Benchmark 独立使用。

Benchmark 通过插件公开的 CLI、API 或其他稳定入口运行测试，不直接调用插件内部实现。
