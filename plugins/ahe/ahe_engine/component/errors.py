"""组件模式的共享异常与 LLM 调用类型别名。"""
from __future__ import annotations

from typing import Callable

LLMCall = Callable[..., tuple[str, int, float]]


class ComponentModeError(RuntimeError):
    """组件模式配置或候选非法时抛出。"""
