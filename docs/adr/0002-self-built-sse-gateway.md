# 自建轻量 SSE gateway，不用官方 langgraph-api

前后端通信不走官方方案（内嵌 LangGraph Server + 前端 `@langchain/langgraph-sdk`），而是自建轻量 SSE gateway：FastAPI `StreamingResponse` 手写 SSE 帧，前端自写 fetch 解析。官方 langgraph-api 要求 `langgraph.json` 与 auth、依赖重，对本地个人部署明显过度；自建的帧格式照搬 DeerFlow 的 `format_sse`，从而保留后续无缝升级到官方 SDK 客户端的余地。

## Status

accepted（曾一度定为「内嵌 LangGraph Server」，后推翻）

## 考虑过的选项

- **官方 langgraph-api + `@langchain/langgraph-sdk`**（首版决策）：协议最标准、未来兼容性最好，但引入 `langgraph.json`、auth 与一大坨传递依赖，本地个人 MVP 代价过高。
- **Next.js rewrites 代理后端**：能省掉 CORS，但 dev 模式下会缓冲 SSE 流，直接破坏逐 token 输出。
