"""契约测试：默认中间件链。

这条断言原先住在 ``test_react.py``（测 ReAct 执行器装配的那个模块）。T16 下线 Plan-Exec 时
``make_react_agent`` 一起删了——它唯一的生产使用者就是 Plan-Exec 的执行单元——但**这条断言
与那条路径无关**：``build_default_middlewares`` 是主 Agent 与子 agent 共用的链，顺序错了
（比如把 LoopDetection 放到 TerminalResponse 后面）症状会非常隐蔽。所以把它单独留成这个文件。

``test_lead_agent.py`` 那几条是拿 ``build_default_middlewares()`` 跟自己比（链 = 默认链 + 追加项），
它们抓不到**默认链自己**被调序——这条抓的就是那个。
"""

from evaharness.agents.middlewares import build_default_middlewares


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
