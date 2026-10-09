"""业务层的建图入口：图与模型都在**请求路径**上现建。

**为什么挪到这里**：原先是 lifespan 各建一张图、所有请求共享。而
:class:`~evaharness.agents.middlewares.loop_detection.LoopDetectionMiddleware` 与
:class:`~evaharness.agents.middlewares.terminal_response.TerminalResponseMiddleware`
把追踪状态存在**实例字段**里（其余五个中间件无状态），于是同一个会话并发两个请求会
互相污染——实测：各调一次工具却被投了一次假的「循环」警告；后启动的那个 run 的
``before_agent`` 会清掉先启动那个的重试预算。可达路径很普通：前端把 ``threadId`` 放在
localStorage（同源共享），两个标签页读到同一个 id，各发一条即可触发。

**为什么重建是廉价的**（实测）：``make_lead_agent`` 全套 3.10 ms、
``make_subagent_graph`` 2.20 ms、``create_chat_model`` 0.39 ms。``ChatOpenAI`` 构造时
不开 socket，且 langchain-openai 按 ``(base_url, timeout, socket_options)`` 缓存默认
httpx 客户端——重建模型复用同一个连接池。

**记忆不受影响**：记忆在 checkpointer 里，不在图里。两次请求各建全新图 + 共享
checkpointer，第 2 轮仍看得到第 1 轮（见 ADR-0011）。

**刻意不做缓存**：3.1 ms 不值得省，而且按配置指纹缓存会把共享的中间件实例原样带回来
——那正是这次要消灭的东西。

本模块**不持有任何图或模型**：只持有跨请求**必须**共享的建图参数。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.chat_models import BaseChatModel
from langgraph.graph.state import CompiledStateGraph

from app.prompts import EXECUTOR_PROMPT, PLANNER_PROMPT, REPLANNER_PROMPT, SUBAGENT_PROMPT, SYSTEM_PROMPT
from app.tools.present_files import PRESENT_FILES_NAME
from evaharness.agents.lead_agent import (
    build_lead_middlewares,
    make_lead_agent,
    make_subagent_graph,
    make_task_tool,
)
from evaharness.agents.options import RunOptions
from evaharness.agents.plan_exec import (
    Planner,
    Replanner,
    TaskExecutor,
    make_planner,
    make_react_executor,
    make_replanner,
)
from evaharness.config.app_config import AppConfig
from evaharness.models.factory import create_chat_model
from evaharness.skills import Skill, render_skills_section

__all__ = ["AgentFactory", "PlanRoles"]

ModelFactory = Callable[[str | None, "RunOptions | None"], BaseChatModel]
"""模型构造接缝：吃 profile 名（``None`` = 主 profile）与这次运行的参数，返回模型实例。

第二个参数是 ADR-0011 预留的那个位置——「每请求参数从哪来」现在有了契约
（:class:`~evaharness.agents.options.RunOptions`）。**摘要模型传 ``None``**：压缩是内部辅助
调用，不该继承用户这一轮选的思考档位。
"""


def _subagent_tools(tools: Sequence[Any]) -> list[Any]:
    """子 agent 的工具集：**剔除** ``present_files``。

    子 agent 用 ``ainvoke`` 跑，没有 custom 流模式，于是它那张图的 ``runtime.stream_writer``
    是个 no-op——工具在里面 emit 的东西**石沉大海**：不抛异常、不报错，只是卡片永远不出现。
    这种「静默失效」比报错难查一个量级，所以在这里**结构性地**剔掉，而不是在工具里加一句
    运行期判断（那种判断本身也可能写错，而且会把「为什么不行」散到两个地方）。

    语义上也对：交付物由主 Agent 统一交给用户。子 agent 只把「文件写到哪了」回报给它。
    """
    return [tool for tool in tools if getattr(tool, "name", None) != PRESENT_FILES_NAME]


@dataclass(frozen=True, slots=True)
class PlanRoles:
    """Plan-Exec 的三件套（封存路径，见 ADR-0010 与 ADR-0011）。

    三者**必须一起换**：``make_react_executor`` 在装配期就把图闭包进了 ``execute``，
    所以只换 planner / replanner 等于没换——留下一张长命的子图，它的中间件桶继续
    无界增长。
    """

    planner: Planner
    executor: TaskExecutor
    replanner: Replanner


class AgentFactory:
    """按请求构建图与角色；自己不持有任何图或模型。

    持有的三样都是**跨请求必须共享**的：配置、业务工具、checkpointer。

    - ``checkpointer`` 尤其不能每请求建：``AsyncSqliteSaver.__init__`` 要活跃事件循环，
      只能在 lifespan 那个协程里构造（ADR-0007）。
    - ``tools`` 同样跨请求共享，所以**业务工具必须无状态**——每请求建图不会复制它们，
      一个带计数器的工具照样会把并发洞种回来。
    """

    def __init__(
        self,
        app_config: AppConfig,
        tools: Sequence[Any],
        checkpointer: Any | None = None,
        *,
        model_factory: ModelFactory | None = None,
        skills: Sequence[Skill] = (),
    ) -> None:
        """保存建图参数，不做任何构建。

        Args:
            app_config: 三层配置。建图要的参数（摘要配置、模型 profile）都在这里。
            tools: 业务工具（``app.gateway.app.BUSINESS_TOOLS``）。
            checkpointer: 长命的检查点器；``None`` 表示这次构建不接记忆——只在测试与
                启动期 smoke build 里出现。
            model_factory: 模型构造接缝。缺省走 :func:`create_chat_model`；测试靠它注入
                假模型，从而能对**真工厂**断言「每请求都建了新图」。

                **别在这里改** ``timeout`` / ``http_client`` 这类参与 httpx 客户端缓存键
                的参数：``httpx.Timeout`` 不可哈希时会绕过缓存、每次新建客户端，连接池
                会按请求泛滥（ADR-0011 硬约束）。同理，模型类必须能无连接构造。
        """
        self.app_config = app_config
        self.tools = list(tools)
        self.checkpointer = checkpointer
        # 技能清单**渲染一次**存下来：它的内容与这次构建绑定，而 `_prompt` 会被调用三次
        # （主图 / 子图 / 执行单元）。空技能时它是 `""`，于是 `_prompt` 原样返回 base。
        self._skill_section = render_skills_section(list(skills))
        self._model_factory: ModelFactory = model_factory if model_factory is not None else self._create_model

    def _prompt(self, base: str) -> str:
        """把技能清单接到基础提示词后面——**空清单时逐字节原样返回**。

        「空时必须一字不差」不是洁癖：它拼在 `system_prompt` 后面，多一点换行就等于所有没写技能的
        部署都拿到一份与从前不同的提示词，前缀缓存白废、`test_lead_agent` 那两条断言也会红。

        **为什么拼字符串而不是用中间件往 messages 里插一条**：`tests/test_react.py` 硬编码了默认
        中间件链的名字，`tests/test_lead_agent_memory.py` 断言模型收到 10 条消息——插消息会把后者
        顶破，加中间件会把前者顶破。拼进 `system_prompt` 两条都不动（system 那条只是变长）。
        """
        return f"{base}\n\n{self._skill_section}" if self._skill_section else base

    def lead(self, options: RunOptions | None = None) -> CompiledStateGraph:
        """建主 Agent 的图，连带它专属的子 agent 图与 ``task`` 工具。

        子 agent 图**必须跟着一起重建**：``make_task_tool`` 把子图闭包进工具、工具再进
        主图。只重建主图的话，子图里那批中间件实例仍然长命——子 agent 的桶继续无界
        增长，并发污染也照样发生。

        ``options`` 是这次运行的参数（思考档位等）。**一个模型实例同时喂给子图与主图**，
        所以子 agent 自动跟随同一份参数——将来若要拆开，记得两边都要动。

        **``self.tools`` 里本来就含 MCP 工具**（启动期装载进 ``app.state``，见
        `app.gateway.mcp`）。所以它们**自动**同时进子图与主图——T13 那层「每请求才有」的
        ``extra_tools`` 就是因为这个语义才存在的，MCP 工具变成启动期常量之后它没必要了。
        子图仍走 ``_subagent_tools``（剔掉 ``present_files``）。
        """
        model = self._model_factory(None, options)
        subagent = make_subagent_graph(
            model,
            _subagent_tools(self.tools),
            system_prompt=self._prompt(SUBAGENT_PROMPT),
            checkpointer=self.checkpointer,
        )
        return make_lead_agent(
            model,
            [*self.tools, make_task_tool(subagent)],
            system_prompt=self._prompt(SYSTEM_PROMPT),
            middleware=self._lead_middlewares(model),
            checkpointer=self.checkpointer,
        )

    def plan_exec(self, options: RunOptions | None = None) -> PlanRoles:
        """建 Plan-Exec 的三件套（封存路径）。

        与 :meth:`lead` 一样是每请求一次：执行单元在构造时建图并闭包进 ``execute``，
        所以「重建」只能通过重建执行单元本身完成。``options`` 同样一个实例喂给三者，
        与主路径保持同一口径。

        ``self.tools``（含 MCP 工具）只喂**执行单元**——planner / replanner 走的是
        ``with_structured_output``，它们不调工具。
        """
        model = self._model_factory(None, options)
        return PlanRoles(
            planner=make_planner(model, system_prompt=PLANNER_PROMPT),
            executor=make_react_executor(
                model,
                self.tools,
                system_prompt=self._prompt(EXECUTOR_PROMPT),
                checkpointer=self.checkpointer,
            ),
            replanner=make_replanner(model, system_prompt=REPLANNER_PROMPT),
        )

    def _lead_middlewares(self, model: BaseChatModel) -> list[AgentMiddleware]:
        """lead 的中间件链：**每次调用都是全新实例**。

        这是并发污染被消除的**充分条件**——两个请求共享一份链，循环检测的账本与兜底
        重试的预算就会互相踩。所以这里既不缓存链，也不缓存链上的任何对象。

        摘要模型只在「开了压缩**且**配了单独的 profile」时才建：``summary_model=None``
        会让 :func:`build_lead_middlewares` 直接复用主模型，省掉一次无谓的构造。
        """
        summarization = self.app_config.agent.summarization
        summary_model = None
        if summarization.enabled and summarization.model:
            # 运行参数传 None：压缩是内部辅助调用，让它跟着用户这轮选的思考档位跑没有意义，
            # 只会让每一次「压一下上下文」都变成一次昂贵的思考。
            summary_model = self._model_factory(summarization.model, None)
        return build_lead_middlewares(
            model,
            summarization=summarization,
            summary_model=summary_model,
            max_steps=self.app_config.agent.max_steps,
        )

    def _create_model(self, name: str | None, options: RunOptions | None = None) -> BaseChatModel:
        """缺省模型构造：按 profile 名从配置里建一个（``None`` = 主 profile）。"""
        return create_chat_model(self.app_config, name, options=options)
