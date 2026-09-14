# 准备 PyPTO 生成任务

在本目录启动 agent，发送下面的 prompt。默认使用与优化示例同版本的 MoE，源码和输出目录可按需修改。
Codex 和 Pi 共用此入口，无需先启动 Goal Plus。已有环境或本地 checkout 可补充到 prompt 中。

```text
请读取 ../task-preparation/SKILL.md，使用 pypto-task-preparation 准备一个 PyPTO 生成任务。

源码：https://github.com/hw-native-sys/pypto-lib/blob/b5df050ef2a2e04706b722874f66eb955bd6b4e3/models/deepseek_v4_flash_mtp/moe.py
输出目录：~/pypto-moe-generation
验收平台：CPU simulator，验证编译与正确性。

按照 skill 完成任务构造和准备校验，并提供任务目录及其中的开发 prompt 路径。
本次只准备任务，不生成 kernel 实现，也不启动 Goal Plus 搜索。
```

准备完成后，在输出目录使用生成的 `prompt.md` 启动开发任务。
