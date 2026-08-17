# Benchmarks

本目录包含：

- `coding/`：AI 辅助编码能力评测（自包含 aibench 模块，在该目录安装和运行）。
- `swebench/`：软件工程修复评测。
- `vulnerability/`：漏洞发现与验证评测。
- `cannbench/`：CANN/算子生成、正确性与性能评测。
- `templates/`：各 Benchmark 可复用的报告模板。

每个 Benchmark 独立维护 runner、测试、实验和报告：

```text
benchmarks/<name>/
├── README.md
├── src/
├── tests/
├── configs/
├── scripts/
├── experiments/
└── reports/
```

Benchmark 负责测试集、执行环境和判分，不直接依赖某个插件的内部实现。

公共报告模板提供两种格式：

- [Markdown 模板](templates/report-template.md)：适合编写和代码评审。
- [HTML 模板](templates/report-template.html)：适合正式展示、浏览器查看和打印。

各 Benchmark 可以在模板基础上增加领域专属章节。
