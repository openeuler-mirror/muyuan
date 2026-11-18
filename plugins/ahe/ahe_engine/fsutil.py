"""引擎模块共享的文件系统小工具。"""
from __future__ import annotations

from pathlib import Path

def _read_head(path: Path, *, max_chars: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except OSError:
        return ""
