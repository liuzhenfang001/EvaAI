"""Agent 层：主 Agent（lead agent）、治理中间件、对外事件契约。

``events`` 是这一层唯一「必须稳定」的东西：它是 SSE 层与 RunJournal 共同的输入，
老 journal 的每一行都要能按它解析回来（见 ADR-0021）。
"""
