"""契约测试：ReAct 执行器装配。"""

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph

from evaharness.agents.middlewares import build_default_middlewares
from evaharness.agents.react import make_react_agent


def test_build_default_middlewares_order():
    mws = build_default_middlewares()
    assert [type(m).__name__ for m in mws] == [
        "InputSanitizationMiddleware",
        "LLMErrorHandlingMiddleware",
        "ToolErrorHandlingMiddleware",
        "TokenUsageMiddleware",
        "LoopDetectionMiddleware",
        "TerminalResponseMiddleware",
    ]


def test_make_react_agent_returns_compiled_graph():
    model = FakeListChatModel(responses=["ok"])
    graph = make_react_agent(model, system_prompt="you are helpful")
    assert isinstance(graph, CompiledStateGraph)


def test_make_react_agent_invokes():
    model = FakeListChatModel(responses=["hello world"])
    graph = make_react_agent(model)
    result = graph.invoke({"messages": [HumanMessage("hi")]})
    assert result["messages"][-1].content == "hello world"


def test_make_react_agent_custom_middleware_passthrough():
    model = FakeListChatModel(responses=["ok"])
    graph = make_react_agent(model, middleware=[])
    assert isinstance(graph, CompiledStateGraph)
