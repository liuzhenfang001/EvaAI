# EvaAI

企业级可用标准的**个人 AI 助手**。顶层是一个 **ReAct 主 Agent**（`lead agent`）——用户直接与它对话，它自己决定调什么工具、要不要派子 agent；**Plan-Exec 编排**仍然保留但已封存（见 [ADR-0010](docs/adr/0010-lead-agent-as-default-path.md)）。

核心是 **harness 工程思想**：框架层（harness）与业务层（app）分离，harness 不关注业务、只为业务提供可扩展性支持。整体层级与框架严格参照本地参考项目 DeerFlow，但针对「个人助手、本地部署」大幅裁剪。

## 目录结构

```
backend/
├── packages/harness/evaharness/   # 框架层（业务无关，可插拔机制）
│   ├── agents/lead_agent/         # ★ 主 Agent：ReAct 图 + 事件流 + 子 agent 派发
│   ├── agents/plan_exec/          # Plan-Exec 编排（封存；路由 /chat/plan/stream）
│   ├── checkpoint/                # checkpoint 持久化（SQLite）与保留策略
│   ├── journal/                   # RunJournal + 可插拔 store（内存 / JSONL）
│   └── agents/                    # ReAct 执行器装配 + 治理中间件
├── app/                           # 业务层（助手人设、具体工具/技能）
│   ├── gateway/                   # FastAPI + SSE + 每请求建图的 AgentFactory
│   └── tools/                     # 工具注册表 + 文件沙箱 + 文件/联网工具
└── tests/                         # 测试（含 test_harness_boundary.py 强制单向依赖）
frontend/                          # Next.js 前端（会话栏 + 聊天区 + 浮动任务面板）
config.yaml / config.example.yaml  # 主配置（版本化，$ENV 引用）
extensions_config.json             # 扩展（MCP / skills / middlewares）
.env                               # 密钥
data/                              # 运行时数据（SQLite checkpoint + data/journal/<会话>/，已 gitignore）
backend/.evaai/                    # 文件沙箱的运行期数据（input/output/workspace/skills-view，已 gitignore）
```

## 环境要求

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)（依赖与虚拟环境管理）
- Node.js >= 20 与 npm（前端）

## 快速开始

```bash
# 1. 用独立虚拟环境安装依赖（backend/.venv，不污染全局）
cd backend
uv sync

# 2. 准备配置
cp ../config.example.yaml ../config.yaml
cp ../.env.example ../.env        # 填入 DEEPSEEK_API_KEY

# 3. 跑测试
uv run pytest

# 4. 启动后端（端口 8010；8001/3000 被参照项目 DeerFlow 占着）
uv run uvicorn app.gateway.app:app --port 8010

# 5. 启动前端（另开终端，端口 3010；package.json 里已固定）
cd ../frontend && npm install && npm run dev
```

浏览器打开 <http://localhost:3010>，发「现在几点？」应看到：**直接流式吐字**，没有「规划中…」的等待；连问「我叫什么」会发现它记得上一轮说过的名字。左侧会话栏可开新对话、在历史对话间切换、重命名或删除；**刷新页面或重启后端后，当前会话的整段对话都会重建**（记录落在 `data/journal/<会话>/`）。

助手气泡把 Agent 的多轮过程**折起来**：当前那一轮灰色实时打字，工具结果回来、下一轮开始时自动收起成「第 N 步 · 调用了什么」的一行（点得开）；最终回复正常显示，与过程一眼分得开。用户消息右对齐、助手消息左对齐，气泡宽度随文本长短变化。右侧的任务编排面板**暂时下线**（组件与样式都还在，由 `page.tsx` 的 `SHOW_TASK_PANEL` 开关控制）。

想跑封存的 Plan-Exec 就用它的路由：`POST /chat/plan/stream`，事件流仍是 `run_start` → `plan` → `task_start` → … → `summary`。

## 开发约定

- **TDD**：harness 层强制红-绿-重构（先写契约测试，再实现）。
- **单向依赖**：`app` 可 import `evaharness`，反向禁止（由 `tests/test_harness_boundary.py` 卡死）。见 [ADR-0001](docs/adr/0001-harness-app-one-way-dependency.md)。
- **可插拔点**：统一用 `evaharness.reflection` 的 `resolve_class`（类）/ `resolve_variable`（实例，如工具），配置里写 `"module.path:attr"`，零 DI 容器。

## 工具集

agent 能做什么由 `config.yaml` 的 `tools:` 决定（见 [ADR-0012](docs/adr/0012-tool-registry-and-file-sandbox.md)）。**加一个工具 = 写一个 `@tool` + 加一项配置，不改框架代码**：

```yaml
tools:
  - name: my_tool
    use: app.tools.mine:my_tool        # 也可以指向工厂：app.tools.web:make_web_search
```

`use` 指向工厂时，同一项里的其它键就是工厂的关键字参数（在启动期绑进闭包，模型看不到也改不了）。`name` 必须与工具自己的 `.name` 一致；全表不许重名、不许占用 `task`——**这几条在启动期校验，写错就拒绝启动**。

工具看到的路径是**虚拟路径**，宿主目录藏在 `backend/.evaai/` 后面：

| 虚拟 | 用途 | 权限 |
|---|---|---|
| `/workspace` | 工作台，相对路径的落点 | 读写 |
| `/output` | 交付给用户的成品 | 读写 |
| `/input` | 用户上传的原件 | 只读 |
| `/skills` | 技能说明文件 | 只读 |

联网工具的两个 key 都是**可选**的：web_search 走 Tavily，不填 `TAVILY_API_KEY` 就用它的无 key 模式（能用，但有速率上限）；web_fetch 走 Jina Reader，不填 `JINA_API_KEY` 也能用（同样只是速率受限）。
- **配置**：三层（.env / config.yaml / extensions_config.json），启动加载 + `config_version` 校验，无热重载。
- **前端包管理**：原定 pnpm，因本机 corepack 权限受限（EPERM）改用 npm；`package-lock.json` 为准。
- **外网访问**：本机 `gh` 与 `npm install` 需走本地代理 `127.0.0.1:7890`。
- **端口**：后端 8010、前端 3010——8001 / 3000 留给本机的参照项目 DeerFlow。前端访问后端的地址只写在 [frontend/src/core/api/config.ts](frontend/src/core/api/config.ts) 一处，要换地址就在 `frontend/.env.local` 里设 `NEXT_PUBLIC_API_URL`（Next.js 只从前端目录读环境文件，仓库根的 `.env` 是给后端用的）。

## 文档

- [CONTEXT.md](CONTEXT.md) —— 项目词汇表（术语与用词约定）。
- [docs/adr/](docs/adr/) —— 架构决策记录。

## 垂直切片

工作按端到端打穿的**垂直切片**交付：

- [x] **T1**（#1）端到端最小聊天链路：浏览器发消息 → 流式回复 + 调用一个纯函数工具
- [x] **T2**（#2）Plan-Exec 编排（单任务）：planner 出计划 → ReAct 执行单元逐个执行 → replanner 判定 → 汇总，全程可在计划面板观察
- [x] **T3**（#4）subagent 并行 + 运行时间线：无依赖任务按批并行派发（ADR-0008），RunJournal 落盘并支持按 run_id 回放
- [x] **T4**（#3）工具集：文件读写与检索（六个工具，跑在虚拟路径的沙箱里）+ web_search/web_fetch + 配置驱动的工具注册表——加一个工具只加文件与配置，不改框架（ADR-0012）
- [ ] **T5**（#5）中断与恢复
- [ ] **T6**（#6）四维度验收收口
- [x] **T7**（#7）会话与对话历史：多会话新建/切换/重命名/删除，整段对话落盘并可在刷新/重启后重建（ADR-0009）
- [x] **T8**（#8）ReAct 主 Agent 取代 Plan-Exec 成为默认路径：直答 + 多轮记忆 + 会话摘要压缩 + 子 agent 派发；Plan-Exec 退到 `/chat/plan/stream` 封存（ADR-0010）
- [x] **T9**（#9）agent 每请求构建：消除单例中间件的并发洞，并把「每请求配置」的门打开（ADR-0011）

已完成的水平里程碑：M0 脚手架 + 边界测试 + 配置加载 + 反射加载；M1 模型工厂 + ReAct 执行器（LangChain `create_agent` + 6 个硬核中间件）。
