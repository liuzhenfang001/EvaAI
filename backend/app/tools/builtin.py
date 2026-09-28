"""内置工具：T1 最小工具集（仅一个纯函数工具打通调用链路）。"""

from __future__ import annotations

from datetime import datetime

from langchain_core.tools import tool


@tool
def get_current_time() -> str:
    """获取当前本地日期和时间（ISO 8601 格式）。"""
    return datetime.now().isoformat(timespec="seconds")


__all__ = ["get_current_time"]
