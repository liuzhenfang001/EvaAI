# EvaAI

企业级可用标准的**个人 AI 助手**，架构为 **Plan-And-Executor**（顶层编排）+ **ReAct**（执行单元）。

核心是 **harness 工程思想**：框架层（harness）与业务层（app）分离，harness 不关注业务、只为业务提供可扩展性支持。整体层级与框架严格参照本地参考项目 DeerFlow，但针对「个人助手、本地部署」大幅裁剪。

## 目录结构

```
backend/
├── packages/harness/evaharness/   # 框架层（业务无关，可插拔机制）
│   ├── agents/plan_exec/          # Plan-Exec 编排（planner / executor / replanner）
│   ├── checkpoint/                # checkpoint 持久化（SQLite）与保留策略
│   └── agents/                    # ReAct 执行器装配 + 治理中间件
├── app/                           # 业务层（助手人设、具体工具/技能）
└── tests/                         # 测试（含 test_harness_boundary.py 强制单向依赖）
frontend/                          # Next.js 前端（聊天区 + 计划面板）
config.yaml / config.example.yaml  # 主配置（版本化，$ENV 引用）
extensions_config.json             # 扩展（MCP / skills / middlewares）
.env                               # 密钥
data/                              # 运行时数据（SQLite checkpoint，已 gitignore）
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

# 4. 启动后端（端口 8001）
uv run uvicorn app.gateway.app:app --port 8001

# 5. 启动前端（另开终端，端口 3000）
cd ../frontend && npm install && npm run dev
```

浏览器打开 <http://localhost:3000>，发「现在几点？」应看到：右侧计划面板出现 planner 拆出的任务卡，卡片状态随执行推进，聊天区流式吐字并显示工具调用标记，最后面板落到汇总。

也可用 VSCode 调试：`.vscode/launch.json` 提供「后端: uvicorn」「前端: full stack」等配置，F5 直接起。


## 开发约定

- **TDD**：harness 层强制红-绿-重构（先写契约测试，再实现）。
- **单向依赖**：`app` 可 import `evaharness`，反向禁止（由 `tests/test_harness_boundary.py` 卡死）。见 [ADR-0001](docs/adr/0001-harness-app-one-way-dependency.md)。
- **可插拔点**：统一用 `evaharness.reflection.resolve_class("module.path:ClassName")`，零 DI 容器。
- **配置**：三层（.env / config.yaml / extensions_config.json），启动加载 + `config_version` 校验，无热重载。
- **前端包管理**：原定 pnpm，因本机 corepack 权限受限（EPERM）改用 npm；`package-lock.json` 为准。
- **外网访问**：本机 `gh` 与 `npm install` 需走本地代理 `127.0.0.1:7890`。

## 文档

- [CONTEXT.md](CONTEXT.md) —— 项目词汇表（术语与用词约定）。
- [docs/adr/](docs/adr/) —— 架构决策记录。

## 垂直切片

MVP 剩余工作按端到端打穿的**垂直切片**交付，对应 GitHub Issue #1–#6：

- [x] **T1**（#1）端到端最小聊天链路：浏览器发消息 → 流式回复 + 调用一个纯函数工具
- [x] **T2**（#2）Plan-Exec 编排（单任务）：planner 出计划 → ReAct 执行单元逐个执行 → replanner 判定 → 汇总，全程可在计划面板观察
- [ ] **T3**（#4）subagent 并行 + 运行时间线
- [ ] **T4**（#3）工具集（文件操作 + web_search/web_fetch）
- [ ] **T5**（#5）中断与恢复
- [ ] **T6**（#6）四维度验收收口

已完成的水平里程碑：M0 脚手架 + 边界测试 + 配置加载 + 反射加载；M1 模型工厂 + ReAct 执行器（LangChain `create_agent` + 6 个硬核中间件）。
