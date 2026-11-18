"""组件模式的向后兼容 facade。

实现已迁移至 ``ahe_engine/component/``；本模块保持历史 import 面
（``from component_mode import run_component_mode``）对既有脚本
与测试可用。
"""
from __future__ import annotations

from ahe_engine.component import (
    ComponentModeError,
    LLMCall,
    _acceptable,
    run_component_mode,
)

__all__ = ["ComponentModeError", "run_component_mode"]
