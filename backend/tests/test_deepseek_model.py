"""契约测试：``DeepSeekChatModel`` 对 ``reasoning_content`` 的保全。

**为什么需要这个子类**：``langchain_openai`` 三个落点全都不碰这个字段——请求侧的
``_convert_message_to_dict``、非流式响应的 ``_convert_dict_to_message``、流式响应的
``_convert_delta_to_message_chunk``，而且**三个都是模块级函数、覆写不了**。所以只能在调用它们的
三个**方法**上补，这个文件钉的就是那三处。

全部**不打网络**：``_get_request_payload`` 是纯函数，两个响应侧方法喂假 dict 即可。
"""

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from evaharness.models.deepseek import REASONING_CONTENT, DeepSeekChatModel

THOUGHT = "用户问时间，我该调工具"


@pytest.fixture
def model() -> DeepSeekChatModel:
    # 不打网络，所以 key 是占位符。
    return DeepSeekChatModel(model="deepseek-flash", api_key="test", max_tokens=16)


def _completion(reasoning=None, content="答") -> dict:
    message = {"role": "assistant", "content": content}
    if reasoning is not None:
        message[REASONING_CONTENT] = reasoning
    return {
        "id": "x",
        "object": "chat.completion",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }


def _chunk(reasoning=None, content="") -> dict:
    delta = {"role": "assistant", "content": content}
    if reasoning is not None:
        delta[REASONING_CONTENT] = reasoning
    return {
        "id": "x",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "deepseek-flash",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }


# --------------------------------------------------------------------------- #
# 请求侧：把历史里的思考贴回去
# --------------------------------------------------------------------------- #


def test_reasoning_is_replayed_onto_the_assistant_message(model):
    """不回放的后果：模型看不见自己上一轮怎么想的；按 DeepSeek 文档，带 tools 时还可能 400。"""
    original = [
        HumanMessage("现在几点"),
        AIMessage(
            content="",
            additional_kwargs={REASONING_CONTENT: THOUGHT},
            tool_calls=[{"name": "get_time", "args": {}, "id": "c1"}],
        ),
    ]

    payload = model._get_request_payload(original)

    assistant = payload["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant[REASONING_CONTENT] == THOUGHT
    assert payload["messages"][0] == {"content": "现在几点", "role": "user"}


def test_a_noop_when_the_model_never_thought(model):
    payload = model._get_request_payload([HumanMessage("hi"), AIMessage(content="你好")])
    assert REASONING_CONTENT not in payload["messages"][1]


def test_nothing_is_replayed_when_the_counts_disagree(model, monkeypatch):
    """**长度不等就整段放弃**。

    那是 ``_use_responses_api`` 那条分支（请求体里根本没有 ``messages`` 键）、或上游将来改了
    行为。宁可丢字段，也不要把某一轮的思考贴到另一轮身上——那种错配是静默的，而且会把模型带偏。
    """
    original = [HumanMessage("a"), AIMessage(content="b", additional_kwargs={REASONING_CONTENT: THOUGHT})]

    real = DeepSeekChatModel.__bases__[0]._get_request_payload
    monkeypatch.setattr(
        DeepSeekChatModel.__bases__[0],
        "_get_request_payload",
        lambda self, input_, *, stop=None, **kw: {"messages": [{"role": "user", "content": "只剩一条"}]},
    )
    try:
        payload = model._get_request_payload(original)
    finally:
        monkeypatch.setattr(DeepSeekChatModel.__bases__[0], "_get_request_payload", real)

    assert REASONING_CONTENT not in payload["messages"][0]


def test_nothing_is_replayed_when_there_is_no_messages_key(model, monkeypatch):
    """responses-API 那条分支把消息放在 ``input`` 里，不是 ``messages``。"""
    monkeypatch.setattr(
        DeepSeekChatModel.__bases__[0],
        "_get_request_payload",
        lambda self, input_, *, stop=None, **kw: {"input": [{"role": "assistant"}]},
    )
    payload = model._get_request_payload(
        [AIMessage(content="b", additional_kwargs={REASONING_CONTENT: THOUGHT})]
    )
    assert "messages" not in payload  # 没炸，而且没有乱写


# --------------------------------------------------------------------------- #
# 响应侧：非流式
# --------------------------------------------------------------------------- #


def test_non_streaming_keeps_the_reasoning(model):
    result = model._create_chat_result(_completion(reasoning="我想了想"))

    message = result.generations[0].message
    assert message.additional_kwargs[REASONING_CONTENT] == "我想了想"
    assert message.content == "答", "正文照旧"


def test_non_streaming_without_reasoning_does_not_invent_one(model):
    result = model._create_chat_result(_completion())
    assert REASONING_CONTENT not in result.generations[0].message.additional_kwargs


# --------------------------------------------------------------------------- #
# 响应侧：流式
# --------------------------------------------------------------------------- #


def test_streaming_chunk_keeps_the_reasoning(model):
    """**这一条最要紧**：没有它，思考只能等整段说完才出现，而它最大的价值是「边说边想」。"""
    generation = model._convert_chunk_to_generation_chunk(_chunk(reasoning="嗯"), AIMessageChunk, None)

    assert generation is not None
    assert generation.message.additional_kwargs[REASONING_CONTENT] == "嗯"


def test_streaming_chunk_of_ordinary_content_is_untouched(model):
    generation = model._convert_chunk_to_generation_chunk(_chunk(content="答"), AIMessageChunk, None)

    assert generation is not None
    assert REASONING_CONTENT not in generation.message.additional_kwargs
    assert generation.message.content == "答"


def test_streaming_chunks_accumulate_because_langchain_concatenates(model):
    """每段增量单独存，靠 langchain 的 ``merge_dicts`` 拼起来——这条钉住那个前提。

    如果哪天 ``additional_kwargs`` 的合并变成「覆盖」，思考就只剩最后一段，而且**不会报错**。
    """
    first = model._convert_chunk_to_generation_chunk(_chunk(reasoning="我"), AIMessageChunk, None)
    second = model._convert_chunk_to_generation_chunk(_chunk(reasoning="想"), AIMessageChunk, None)

    merged = first.message + second.message

    assert merged.additional_kwargs[REASONING_CONTENT] == "我想"
