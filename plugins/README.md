# Plugins

本目录包含：

- `goal-plus/`：持久目标、验证、候选隔离、回滚和交付。
- [inference_scaling/](inference_scaling/README.md)：语言模型的 training-free 推理扩展、推理服务与评测。
- [self-speculation/](self-speculation/README.md)：流式推理分叉、工具调用解码与 draft 反馈的独立 Python 库。
- [pi-speculative-action/](pi-speculative-action/README.md)：Pi 工具投机执行与缓存复用扩展。

每个插件应能独立构建、测试和使用。具体 Benchmark 的实验和报告放在对应的 `benchmarks/<name>/` 中。
