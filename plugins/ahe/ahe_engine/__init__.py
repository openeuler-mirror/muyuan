"""AHE 引擎包 —— ahe.py CLI 背后的实现模块。

布局：
    api_config.py     LLM API 发现（环境变量 / env 文件 / Pi / Codex）
    llm_client.py     极简 OpenAI 兼容 chat 客户端
    fsutil.py         引擎模块共享的文件系统小工具
    artifacts.py      完成报告的 artifact bundle 发布
    single_mode.py    单 prompt 文件 harness 优化循环
    evaluator_gen.py  基于 LLM 的评估器生成（+ AHE-OBJECTIVE）
    preflight.py      只读预检与 harness 安装
    export.py         将被接受候选导出为最终 harness
    cli.py            argparse 入口（python3 ahe.py ...）
    component/        组件模式优化（manifest + adapter）
"""
from pathlib import Path

# plugins/ahe —— 仓库相对默认值（artifact workspace 等）的锚点
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
