"""``capture_turns`` 的契约：只取真实用户消息 + 最终助手回复，按 watermark 扫窗口。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from evaharness.memory.capture import capture_turns, content_text


def _human(text: str, *, message_id: str = "h", hidden: bool = False) -> HumanMessage:
    return HumanMessage(
        content=text,
        id=message_id,
        additional_kwargs={"hide_from_ui": True} if hidden else {},
    )


def _ai(text: str, *, message_id: str = "a", **kwargs) -> AIMessage:
    return AIMessage(content=text, id=message_id, **kwargs)


def test_a_plain_exchange_becomes_one_turn():
    result = capture_turns([_human("请记住我偏好中文回答", message_id="h1"), _ai("好的", message_id="a1")])

    assert len(result.turns) == 1
    assert result.turns[0].user_text == "请记住我偏好中文回答"
    assert result.turns[0].assistant_text == "好的"
    assert result.turns[0].last_message_id == "a1"
    assert result.last_complete_ai_id == "a1"


def test_tool_round_trips_are_excluded_and_the_final_answer_is_kept():
    messages = [
        _human("这个问题要查一下", message_id="h1"),
        _ai("", message_id="a1", tool_calls=[{"name": "search", "args": {}, "id": "c1"}]),
        ToolMessage(content="搜索结果", tool_call_id="c1", id="t1"),
        _ai("查到了：结论是 X", message_id="a2"),
    ]

    result = capture_turns(messages)

    assert len(result.turns) == 1
    assert result.turns[0].assistant_text == "查到了：结论是 X"
    assert result.turns[0].last_message_id == "a2"


def test_hidden_human_messages_are_not_user_turns():
    messages = [
        _human("真正的用户消息", message_id="h1"),
        _human("<memory>背景</memory>", message_id="m1", hidden=True),
        _ai("回答", message_id="a1"),
    ]

    result = capture_turns(messages)

    assert len(result.turns) == 1
    assert result.turns[0].user_text == "真正的用户消息"


def test_pleasantries_do_not_create_turns_but_do_advance_the_watermark():
    result = capture_turns([_human("谢谢", message_id="h1"), _ai("不客气", message_id="a1")])

    assert result.turns == ()
    assert result.last_complete_ai_id == "a1"


def test_too_short_user_messages_are_filtered():
    result = capture_turns([_human("嗯", message_id="h1"), _ai("好的", message_id="a1")], min_chars=3)

    assert result.turns == ()
    assert result.last_complete_ai_id == "a1"


def test_after_id_only_returns_later_turns():
    messages = [
        _human("第一轮", message_id="h1"),
        _ai("一", message_id="a1"),
        _human("第二轮", message_id="h2"),
        _ai("二", message_id="a2"),
    ]

    result = capture_turns(messages, after_id="a1")

    assert [turn.user_text for turn in result.turns] == ["第二轮"]
    assert result.last_complete_ai_id == "a2"


def test_an_unknown_after_id_falls_back_to_the_last_turn_not_the_whole_history():
    """watermark 被摘要删掉时，只回退到最后一个完整轮次——不整段重扫。"""
    messages = [
        _human("很久以前的一轮", message_id="h1"),
        _ai("旧回答", message_id="a1"),
        _human("最近一轮", message_id="h2"),
        _ai("新回答", message_id="a2"),
    ]

    result = capture_turns(messages, after_id="被摘要删掉的id")

    assert [turn.user_text for turn in result.turns] == ["最近一轮"]


def test_an_unanswered_turn_does_not_advance_the_watermark():
    messages = [
        _human("第一轮", message_id="h1"),
        _ai("一", message_id="a1"),
        _human("被取消的一轮", message_id="h2"),
    ]

    result = capture_turns(messages)

    assert [turn.user_text for turn in result.turns] == ["第一轮"]
    assert result.last_complete_ai_id == "a1"


def test_content_blocks_are_flattened():
    ai = AIMessage(
        content=[{"type": "text", "text": "块状"}, {"type": "text", "text": "回答"}],
        id="a1",
    )

    result = capture_turns([_human("问题够长", message_id="h1"), ai])

    assert result.turns[0].assistant_text == "块状回答"
    assert content_text(ai.content) == "块状回答"


def test_the_error_fallback_message_is_not_a_final_answer():
    fallback = _ai(
        "The model completed the tool run but returned no final response.",
        message_id="a1",
        additional_kwargs={"evaharness_error_fallback": True},
    )

    result = capture_turns([_human("有实质内容的问题", message_id="h1"), fallback])

    assert result.turns == ()
    assert result.last_complete_ai_id is None


def test_max_turns_keeps_the_most_recent_ones():
    messages = []
    for index in range(5):
        messages.append(_human(f"第 {index} 轮的问题", message_id=f"h{index}"))
        messages.append(_ai(f"第 {index} 轮的回答", message_id=f"a{index}"))

    result = capture_turns(messages, max_turns=2)

    assert [turn.user_text for turn in result.turns] == ["第 3 轮的问题", "第 4 轮的问题"]
    assert result.last_complete_ai_id == "a4"
