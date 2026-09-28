"""gateway SSE 流式集成测试（FakeListChatModel，不依赖真实模型/配置）。"""

import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage

from app.gateway.sse import sse_consumer
from evaharness.agents.react import make_react_agent


def _parse(frame):
    """解析单个 SSE 帧为 (event, data)。"""
    event = None
    data = None
    for line in frame.strip("\n").split("\n"):
        if line.startswith("event: "):
            event = line[len("event: ") :]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: ") :])
    return event, data


async def _collect(graph, content):
    frames = []
    async for frame in sse_consumer(graph, [HumanMessage(content=content)]):
        frames.append(frame)
    return frames


@pytest.mark.asyncio
async def test_sse_consumer_emits_messages_then_end():
    model = FakeListChatModel(responses=["hello world"])
    graph = make_react_agent(model)

    frames = await _collect(graph, "hi")
    events = [_parse(f)[0] for f in frames]

    assert "messages" in events
    assert events[-1] == "end"

    # 第一条 messages 帧 data = [chunk_dict, metadata]
    idx = events.index("messages")
    event, data = _parse(frames[idx])
    assert event == "messages"
    assert isinstance(data, list) and len(data) == 2
    chunk, metadata = data
    assert isinstance(chunk, dict) and "content" in chunk
    assert isinstance(metadata, dict)

    # end 帧 data 为 null
    assert _parse(frames[-1]) == ("end", None)
