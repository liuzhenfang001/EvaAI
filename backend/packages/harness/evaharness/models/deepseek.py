"""DeepSeek 模型适配器：让 ``reasoning_content`` 能回放、也能被看见。

`ChatOpenAI` **三个落点全都不碰 ``reasoning_content``**——它自己在模块 docstring 里就写明了：
「Non-standard response fields added by third-party providers (e.g., ``reasoning_content``) are
**not** extracted or preserved. ... use the corresponding provider-specific LangChain package
instead」。我们没有那个包（那是 ``langchain_deepseek``），所以按它建议的做：**写一个 provider
专用子类**。

丢在哪三处（本机 ``langchain_openai 1.6.6`` 的 ``chat_models/base.py``）：

===============  ==========================  ============================================
落点              位置                         症状
===============  ==========================  ============================================
请求侧            ``_convert_message_to_dict`` 历史里 assistant 的 ``reasoning_content`` 不进请求体
                （模块级函数，L472）
非流式响应        ``_convert_dict_to_message`` 响应里的 ``reasoning_content`` 不进
                （模块级函数，L218）          ``additional_kwargs``
流式响应          ``_convert_delta_to_message_chunk`` 同上，而且**流式下前端根本看不到思考**
                （模块级函数，L554）
===============  ==========================  ============================================

三个都是**模块级函数、覆写不了**，所以只能在调用它们的**方法**上补：``_get_request_payload`` /
``_create_chat_result`` / ``_convert_chunk_to_generation_chunk``。

**这里刻意不管「思考开关」**：那是通用的事（任何 OpenAI 兼容模型都能用 ``extra_body.thinking``），
放 :mod:`evaharness.models.factory` 里按运行参数翻译，于是本类不需要新增 pydantic 字段——
也就不会撞上 ``build_extra`` 把未知字段塞进 ``model_kwargs``、原样发给 API 的那个陷阱。

.. warning::
   **回放行为只对 V4 系模型成立**（``deepseek-flash`` / ``deepseek-v4-pro``）。契约在代际之间
   **反过来了**：R1 系（``deepseek-reasoner``）要求把 ``reasoning_content`` **删掉**再发，
   带上它反而 400。所以把 ``config.yaml`` 指向 R1 系模型之前，得先关掉这里的行为——
   参照项目 ``langchain_deepseek`` 至今不做请求侧回放，正是因为它的父类「丢掉非标准字段」
   恰好就是 R1 要的「删掉」。
   本机实测（2026-10-08，``deepseek-flash``）：带与不带都不 400，即 V4 的强校验当前没生效；
   但**照文档的契约走**比依赖服务端的宽容稳。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import LanguageModelInput
from langchain_core.messages import AIMessage
from langchain_core.messages.ai import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk, ChatResult
from langchain_openai import ChatOpenAI

REASONING_CONTENT = "reasoning_content"
"""存进 ``additional_kwargs`` 的键名。用官方字段名本身，不另起别名。

顺带的好处：``langchain_core`` 自己认识这个键（``messages/base.py`` 的
``_extract_reasoning_from_additional_kwargs`` 注释里点名了 DeepSeek），而且它的 ``merge_dicts``
对字符串值是**拼接**而不是覆盖——所以流式每来一段就往这里塞一段，被 langgraph 聚合时会自动接成
完整的一段思考，不需要我们维护累积逻辑。
"""


class DeepSeekChatModel(ChatOpenAI):
    """``ChatOpenAI`` + DeepSeek 的思考内容保全（请求侧回放 + 响应侧提取）。"""

    def _get_request_payload(
        self,
        input_: LanguageModelInput,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict:
        """把历史 assistant 的思考放回请求体。

        不这么做的话，DeepSeek 要么看不到自己上一轮怎么想的，要么（按它的文档）在带 ``tools``
        时因为历史缺 ``reasoning_content`` 直接 400。**实测我们这个账号目前不 400**，但这属于
        「不该依赖服务端当下的宽容」——文档写明的契约就照做，参照项目 DeerFlow 也是这么处理的。
        """
        original = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        _replay_reasoning(payload.get("messages"), original)
        return payload

    def _create_chat_result(self, response: Any, generation_info: dict | None = None) -> ChatResult:
        """非流式：把 ``choices[].message.reasoning_content`` 收进 ``additional_kwargs``。"""
        result = super()._create_chat_result(response, generation_info)
        for generation, choice in zip(result.generations, _choices_of(response)):
            reasoning = (choice.get("message") or {}).get(REASONING_CONTENT)
            if reasoning:
                generation.message.additional_kwargs[REASONING_CONTENT] = reasoning
        return result

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        """流式：把 ``delta.reasoning_content`` 收进 ``additional_kwargs``。

        没有这一步，思考只能等整段说完才出现——而它最大的价值恰恰是「边说边想」。
        补法是**从原始 chunk 里读**再塞进去：真正做转换的
        :func:`~langchain_openai.chat_models.base._convert_delta_to_message_chunk` 是模块级函数，
        覆写不了。
        """
        generation = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if generation is not None and isinstance(generation.message, AIMessageChunk):
            for choice in _choices_of(chunk):
                reasoning = (choice.get("delta") or {}).get(REASONING_CONTENT)
                if reasoning:
                    generation.message.additional_kwargs[REASONING_CONTENT] = reasoning
                break  # 只取第一个 choice，与父类的做法一致
        return generation


def _choices_of(response: Any) -> list[dict]:
    """把 ``dict`` 或 SDK 对象统一成 ``choices`` 列表。

    两个调用点的入参形状不同：``_create_chat_result`` 拿到的是完整响应，而
    ``_convert_chunk_to_generation_chunk`` 拿到的流式 chunk 有时候把 choices 包在
    ``chunk["chunk"]`` 里（``beta.chat.completions.stream`` 那条路）。
    """
    raw = response if isinstance(response, dict) else response.model_dump(warnings=False)
    return raw.get("choices") or raw.get("chunk", {}).get("choices") or []


def _replay_reasoning(payload_messages: Any, original: list[Any]) -> None:
    """按**位置**把 ``reasoning_content`` 贴回请求体里的 assistant 消息。

    为什么按下标 zip 就够：``_get_request_payload`` 里 ``payload["messages"]`` 是对**同一个
    ``messages`` 列表**做的列表推导，条数与顺序都不可能变（本机源码 L2041-2046），而
    ``ChatOpenAI`` 那次覆写只改字段名、不增删不重排。所以这里的 zip 是恒等对齐，不需要参照
    项目那套按 content + tool_call id 的签名匹配。

    **长度不等时整段放弃**：那是 ``_use_responses_api`` 那条分支（请求体里根本没有
    ``messages`` 键）、或上游将来改了行为。宁可丢字段，也不要把某一轮的思考贴到另一轮上——
    那种错配是静默的，而且会把模型带偏。
    """
    if not isinstance(payload_messages, list) or len(payload_messages) != len(original):
        return
    for payload_message, source in zip(payload_messages, original):
        if not isinstance(payload_message, dict) or payload_message.get("role") != "assistant":
            continue
        if not isinstance(source, AIMessage):
            continue
        reasoning = source.additional_kwargs.get(REASONING_CONTENT)
        if reasoning:
            payload_message[REASONING_CONTENT] = reasoning


__all__ = ["REASONING_CONTENT", "DeepSeekChatModel"]
