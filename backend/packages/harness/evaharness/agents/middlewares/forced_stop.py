"""强制收尾的工具函数：剥掉 ``tool_calls`` 并追加说明文本，让模型转成最终回复。

loop_detection 与 step_budget 都要做同一件事——「别再调工具了，给最终答复」——只是
触发条件不同（一个是「重复调用」，一个是「步数用完」）。收在同一处，改一处两边一起
生效，否则又会出现「一个剥干净了、另一个只清了一半」这种最难查的不一致（同
``context.py`` 那处「两处各写一份」的教训）。
"""

from __future__ import annotations


def strip_tool_calls(last_msg, note: str):
    """清空 ``tool_calls`` 元数据，追加说明文本，强制转成普通文本回复。

    ``content`` 可能是三种形状（纯文本 / 分块列表 / 其它），逐种处理，避免把说明文本
    拼进错误的位置。
    """
    content = last_msg.content
    if isinstance(content, str):
        content = f"{content}\n\n{note}" if content else note
    elif isinstance(content, list):
        content = [*content, {"type": "text", "text": note}]
    else:
        content = note
    update: dict = {"tool_calls": [], "content": content}
    additional_kwargs = dict(getattr(last_msg, "additional_kwargs", {}) or {})
    additional_kwargs.pop("tool_calls", None)
    additional_kwargs.pop("function_call", None)
    update["additional_kwargs"] = additional_kwargs
    return last_msg.model_copy(update=update)


__all__ = ["strip_tool_calls"]
