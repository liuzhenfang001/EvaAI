# 复用 LangChain create_agent，不自写 ReAct 循环

ReAct 循环本身不自研，复用 LangChain v1 的 `create_agent`（它编译为 LangGraph 图），`evaharness.agents.react.make_react_agent` 只负责注入 EvaAI 的默认中间件链与默认 state schema。循环的正确性交给上游维护，EvaAI 的所有治理通过 `AgentMiddleware` 链表达，而不是塞进 system prompt。

## 考虑过的选项

- **自写 ReAct 循环**：完全可控、无版本耦合，但要自己承担循环正确性、流式语义、中断等一整套难题，收益不抵成本。
