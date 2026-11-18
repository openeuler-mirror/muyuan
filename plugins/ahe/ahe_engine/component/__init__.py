"""可执行 harness 的组件级 AHE 优化。

manifest 声明允许变更的文件；候选被复制到隔离 workspace，
经校验、外部命令评测后，才会晋级回源 harness。

模块布局：
    errors.py    ComponentModeError + LLMCall 别名
    util.py      共享解析/打印工具
    metrics.py   指标归一化 + objective 解析
    commands.py  manifest/命令解析
    context.py   有界只读上下文构建
    runenv.py    运行环境 + adapter 命令渲染
    debugger.py  外部/内置失败诊断
    patches.py   提案解析、物化、锚点校验
    propose.py   evolve 模型提案步骤
    evaluate.py  候选校验 + 评测执行
    gates.py     keep/revert 接受门禁
    loop.py      run_component_mode campaign 主循环
"""
from .errors import ComponentModeError, LLMCall
from .gates import _acceptable
from .loop import run_component_mode

__all__ = ["ComponentModeError", "LLMCall", "run_component_mode"]
