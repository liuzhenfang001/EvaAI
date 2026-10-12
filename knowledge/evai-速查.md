# EvaAI 速查

## 项目是什么

EvaAI 是一个**本地部署的个人 AI 助手**，按「机制在 harness（`evaharness`）、策略在 app」
两层划分：沙箱、文件工具、技能解析、MCP 装配这些与业务无关的机制放在 harness 里，
而装哪些工具、人设是什么、宿主根在哪儿这些决定放在 app 层。

## 常用命令

- 起服务：`cd backend && uv run uvicorn app.gateway.app:app --port 8010`
- 跑测试：`cd backend && uv run pytest`
- 装依赖：`cd backend && uv sync`

## 几个关键概念

- **虚拟路径**：模型只看得到 `/workspace`、`/output`、`/input`、`/skills`、`/knowledge`
  这几个虚拟目录，宿主的绝对路径永远不进模型上下文。
- **沙箱容器**：`bash` 工具执行的命令跑在每会话一个的长驻 Linux 容器里，容器内的路径就是
  虚拟路径本身，所以文件工具与容器看到的是同一份文件。
- **会话（thread）**：一段连续的对话，由多次运行组成；记忆存在 checkpoint 里，不在图里。

## 支持热线

内部支持请联系 `support@example.internal`，工作日 9:00–18:00。
