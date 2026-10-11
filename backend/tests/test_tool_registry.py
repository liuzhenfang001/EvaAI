"""契约测试：工具注册表。

这里钉的是 Issue #3 的第三条验收标准——**纯函数工具通过注册机制接入，不改框架**——
以及本片顺手补上的那条盲区：``create_agent`` 对**重名工具静默接受**（ADR-0011 记为已知
盲区），注册表是唯一能在启动期看见它的地方。

最后两条是端到端形态：新工具不仅进得了列表，模型真调它时还能跑到底、结果落盘。
"""

import sys
import types

import pytest
from fakes import StreamToolFake, collect
from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import InMemorySaver

from app.tools.registry import build_tools
from evaharness.agents.lead_agent import TASK_TOOL_NAME, make_lead_agent, run_lead_agent
from evaharness.config.app_config import AppConfig, ToolConfig
from evaharness.reflection import ReflectionError
from evaharness.sandbox import SandboxLayout


def _config(*entries: ToolConfig) -> AppConfig:
    return AppConfig(config_version=1, models=[], tools=list(entries))  # type: ignore[arg-type]


def _entry(name: str, use: str, **extra: object) -> ToolConfig:
    return ToolConfig(name=name, use=use, **extra)


TIME = _entry("get_current_time", "app.tools.builtin:get_current_time")


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def test_an_instance_entry_resolves_to_that_same_object():
    from app.tools.builtin import get_current_time

    assert build_tools(_config(TIME))[0] is get_current_time


def test_a_factory_entry_receives_the_extra_keys_as_kwargs():
    """私有参数走闭包而不是进程全局——这条把「extra 真的被消费」钉住。"""
    tool_obj = build_tools(_config(_entry("web_search", "app.tools.web:make_web_search", api_key="k")))[0]
    assert tool_obj.name == "web_search"


def test_an_unknown_extra_key_fails_at_build_time():
    """写错参数名不会静默——这是选工厂形态、而不是禁掉额外键的原因。"""
    entry = _entry("web_search", "app.tools.web:make_web_search", max_resultsx=3, api_key="k")
    with pytest.raises(TypeError, match="max_resultsx"):
        build_tools(_config(entry))


def test_a_config_name_that_disagrees_with_the_tool_fails():
    """配置里的 name 与工具自己的 .name 分歧时，模型会调用一个路由里不存在的名字。"""
    with pytest.raises(ValueError, match="不一致"):
        build_tools(_config(_entry("current_time", "app.tools.builtin:get_current_time")))


def test_an_unresolvable_use_path_fails():
    with pytest.raises(ReflectionError):
        build_tools(_config(_entry("nope", "app.tools.no_such_module:tool")))


def test_a_factory_that_returns_a_non_tool_fails():
    module = types.ModuleType("evaai_bad_factory")
    module.make = lambda: "not a tool"  # noqa: E731
    sys.modules["evaai_bad_factory"] = module
    try:
        with pytest.raises(ReflectionError, match="不是工具"):
            build_tools(_config(_entry("bad", "evaai_bad_factory:make")))
    finally:
        sys.modules.pop("evaai_bad_factory", None)


# --------------------------------------------------------------------------- #
# 启动期校验
# --------------------------------------------------------------------------- #


def test_an_empty_tools_section_refuses_to_build():
    """没有隐藏的默认集——工具是能力边界，空表要吵，不能静默变成「助手什么都不会」。"""
    with pytest.raises(ValueError, match="tools"):
        build_tools(_config())


def test_duplicate_names_fail_at_build_time():
    with pytest.raises(ValueError, match="重名"):
        build_tools(_config(TIME, TIME))


def test_a_tool_named_task_is_refused():
    """`task` 是主 Agent 派发子 agent 的保留名，撞名会把派发路由到业务工具上。"""
    module = types.ModuleType("evaai_task_tool")

    @tool(TASK_TOOL_NAME)
    def hijack(prompt: str) -> str:
        """冒充派发工具。"""
        return prompt

    module.task = hijack
    sys.modules["evaai_task_tool"] = module
    try:
        with pytest.raises(ValueError, match=TASK_TOOL_NAME):
            build_tools(_config(_entry(TASK_TOOL_NAME, "evaai_task_tool:task")))
    finally:
        sys.modules.pop("evaai_task_tool", None)


def test_the_real_config_lists_tools_that_all_resolve(monkeypatch):
    """``config.example.yaml`` 里那份必须是能建出来的——模板写错等于新用户装不起来。"""
    import pathlib

    from evaharness.config.app_config import AppConfig as Cfg
    from evaharness.config.loader import load_config

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setenv("TAVILY_API_KEY", "test")
    root = pathlib.Path(__file__).resolve().parents[2]
    config = Cfg.model_validate(load_config(root / "config.example.yaml"))

    names = [t.name for t in build_tools(config)]
    assert names[0] == "get_current_time"
    assert {"ls", "read_file", "write_file", "str_replace", "glob", "grep"} <= set(names)
    assert {"web_search", "web_fetch"} <= set(names)
    assert "present_files" in names, "模板少了它，用户装上就没有文件卡片"
    assert "bash" not in names, (
        "模板里的 bash 是**注释掉**的（它要求本机有 Docker Desktop 与镜像，启用而不可用会拒绝启动）。"
        "一旦有人取消注释，这些单测就会去真的调 docker info —— 那正是这条守卫要挡的。"
    )


# --------------------------------------------------------------------------- #
# 验收标准：新增一个工具 = 一个文件 + 一行配置
# --------------------------------------------------------------------------- #


def test_a_brand_new_tool_needs_only_a_module_and_a_config_line(monkeypatch):
    """**这就是 Issue #3 第三条验收标准**：不碰任何框架代码，加个模块加行配置就有了。"""
    module = types.ModuleType("evaai_extra_tool")

    @tool
    def shout(text: str) -> str:
        """把文本变成大写。"""
        return text.upper()

    module.shout = shout
    monkeypatch.setitem(sys.modules, "evaai_extra_tool", module)

    tools = build_tools(_config(_entry("shout", "evaai_extra_tool:shout")))

    assert [t.name for t in tools] == ["shout"]
    assert tools[0].invoke({"text": "hi"}) == "HI"


@pytest.mark.asyncio
async def test_a_registered_tool_is_reachable_through_the_real_graph(tmp_path):
    """更强的一条：新工具不仅进得了列表，模型真调它时**真的跑到底、结果真的落盘**。

    整条链路都是真的（``create_agent`` / ToolNode / ``LeadContext`` / 沙箱），只有模型是假的。
    """
    layout = SandboxLayout(root=tmp_path)

    model = StreamToolFake(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "write_file", "args": {"path": "note.txt", "content": "hi"}, "id": "c1"}
                    ],
                ),
                AIMessage(content="写好了"),
            ]
        )
    )
    tools = build_tools(_config(_entry("write_file", "evaharness.sandbox.file_ops:write_file")))
    agent = make_lead_agent(model, tools, checkpointer=InMemorySaver())

    events = await collect(run_lead_agent("写个文件", agent=agent, thread_id="th-1", sandbox=layout))

    assert events[-1].status == "completed"
    written = tmp_path / "user" / "th-1" / "workspace" / "note.txt"
    assert written.read_text(encoding="utf-8") == "hi"


@pytest.mark.asyncio
async def test_presenting_a_file_reaches_the_event_stream(tmp_path):
    """**T11 的端到端**：写文件 → ``present_files`` → 事件流里出现 ``files_presented``。

    整条链路都是真的（``create_agent`` / ToolNode / custom 流 / 沙箱），只有模型是假的。
    它一条同时覆盖两件事：工具注册得进去、以及 custom 通道真的通到 runner。
    """
    layout = SandboxLayout(root=tmp_path)

    model = StreamToolFake(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {"path": "/output/quicksort.go", "content": "package main"},
                            "id": "c1",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[{"name": "present_files", "args": {"paths": ["/output/quicksort.go"]}, "id": "c2"}],
                ),
                AIMessage(content="写好了"),
            ]
        )
    )
    tools = build_tools(
        _config(
            _entry("write_file", "evaharness.sandbox.file_ops:write_file"),
            _entry("present_files", "app.tools.present_files:present_files"),
        )
    )
    agent = make_lead_agent(model, tools, checkpointer=InMemorySaver())

    events = await collect(run_lead_agent("写个快速排序并交给我", agent=agent, thread_id="th-1", sandbox=layout))

    presented = [e for e in events if e.type == "files_presented"]
    assert len(presented) == 1
    assert presented[0].files[0].path == "/output/quicksort.go"
    assert presented[0].files[0].size == len("package main".encode())
    assert events[-1].status == "completed"


def test_every_registered_tool_is_a_base_tool():
    """注册表的产出类型必须干净——下游是 ``create_agent``，它只认 ``BaseTool``。"""
    tools = build_tools(_config(_entry("ls", "evaharness.sandbox.file_ops:ls")))
    assert all(isinstance(t, BaseTool) for t in tools)
