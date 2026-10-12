"""agent 层测试的共用替身。

原先叫 ``plan_exec_fakes``——T16 下线 Plan-Exec 之前，这个文件里同时装着 planner/executor 的
替身与这些通用的假模型。产线删掉之后只剩通用部分，名字跟着改（它从来就不是 plan-exec 专用的：
T16 删完之后是 11 个测试文件 import 它）。

这里装的是**假模型**与最小配置：``ToolCapableFake`` 系（能真走 ``bind_tools``）、
``StructuredFake`` 系（按 schema 分派），以及 ``fake_app_config``。工具拿 runtime 的替身
在 :mod:`tool_fakes`。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool

from evaharness.config.app_config import AppConfig
from evaharness.runtime import RunEvent


def fake_app_config(**summarization: Any) -> AppConfig:
    """最小可用配置。模型 profile 只是占位——用到它的测试都注入 ``model_factory``。"""
    return AppConfig.model_validate(
        {
            "config_version": 1,
            "models": [{"name": "fake", "model": "fake"}],
            "agent": {"summarization": {"enabled": False, **summarization}},
        }
    )


class ToolCapableFake(GenericFakeChatModel):
    """会响应 ``bind_tools`` 的假模型（``GenericFakeChatModel`` 本身不实现它）。"""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any):  # noqa: ANN201
        return self


class StreamToolFake(ToolCapableFake):
    """流式路径也**携带 tool_calls** 的假模型。

    两个坑，都是实测出来的（见 ADR-0010）：

    1. ``GenericFakeChatModel._stream`` 只搬 ``content``，``tool_calls`` 会被丢掉
       （它只特判了遗留的 ``additional_kwargs["function_call"]``）。所以
       :class:`ToolCapableFake` 能驱动纯文本，却**驱动不了工具往返**——工具压根不会被调用，
       而测试不会失败，只会静默地测到一条没有工具调用的路径。
    2. ``content=""`` 的工具调用消息在流式下**一个 chunk 都产不出**，直接
       ``ValueError: No generations found in stream``。所以这里不做按 token 切分，
       一次调用产出一个整块 chunk（测试关心的是「工具被调用了」，不是逐 token 边界）。

    ``args`` 必须是 JSON 字符串：langgraph 会把同一 index 的 ``args`` 片段拼接后再解析。

    顺带记录每一次调用收到的消息（``calls``），好断言「什么进了主对话、什么没进」。
    """

    calls: list = []

    def _stream(  # noqa: ANN001, ANN003
        self,
        messages: Any,
        stop: Any = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        self.calls = [*self.calls, list(messages)]
        message = next(self.messages)
        if isinstance(message, str):
            message = AIMessage(content=message)
        chunk = AIMessageChunk(
            content=message.content,
            id=message.id,
            tool_call_chunks=[
                {
                    "name": call["name"],
                    "args": json.dumps(call.get("args") or {}, ensure_ascii=False),
                    "id": call["id"],
                    "index": index,
                    "type": "tool_call_chunk",
                }
                for index, call in enumerate(message.tool_calls or [])
            ],
        )
        yield ChatGenerationChunk(message=chunk, generation_info={})


class CallRecordingFake(ToolCapableFake):
    """把模型**每一次**调用收到的消息都记下来。

    挂 ``_generate`` 而不是 ``_stream``：非流式路径（``ainvoke`` / ``invoke``，子 agent
    走的就是它）只经过前者。只覆盖 ``_stream`` 的话测试不会报错，只会永远读到空列表。
    """

    calls: list = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003
        self.calls = [*self.calls, list(messages)]
        return super()._generate(messages, stop, run_manager, **kwargs)


class RecordingFake(ToolCapableFake):
    """记录收到的最后一条 HumanMessage，用于断言 prompt 渲染。

    必须挂在 ``_astream`` 上：``GenericFakeChatModel`` 只实现了流式路径，覆盖
    ``_agenerate`` 根本不会被调用（写错了测试会静默地永远拿到 ``None``）。
    """

    last_human: str | None = None

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ANN003
        for message in reversed(list(messages)):
            if isinstance(message, HumanMessage):
                self.last_human = str(message.content)
                break
        async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
            yield chunk


@tool
def echo(text: str) -> str:
    """回显给定文本（占位工具，用于让 bind_tools 真正被调用）。"""
    return text


async def collect(stream: AsyncIterator[RunEvent]) -> list[RunEvent]:
    """把一个事件流收干成列表。"""
    return [event async for event in stream]
