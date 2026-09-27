# EvaAI

企业级可用标准的**个人 AI 助手**，架构为 **Plan-And-Executor**（顶层编排）+ **ReAct**（执行单元）。

核心是 **harness 工程思想**：框架层（harness）与业务层（app）分离，harness 不关注业务、只为业务提供可扩展性支持。整体层级与框架严格参照本地参考项目 DeerFlow，但针对「个人助手、本地部署」大幅裁剪。

## 目录结构

```
backend/
├── packages/harness/evaharness/   # 框架层（业务无关，可插拔机制）
├── app/                           # 业务层（助手人设、具体工具/技能）
└── tests/                         # 测试（含 test_harness_boundary.py 强制单向依赖）
frontend/                          # Next.js 前端（聊天工作区 + 运行时间线）
config.yaml / config.example.yaml  # 主配置（版本化，$ENV 引用）
extensions_config.json             # 扩展（MCP / skills / middlewares）
.env                               # 密钥
```

## 环境要求

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)（依赖与虚拟环境管理）

## 快速开始

```bash
# 1. 用独立虚拟环境安装依赖（backend/.venv，不污染全局）
cd backend
uv sync

# 2. 准备配置
cp ../config.example.yaml ../config.yaml
cp ../.env.example ../.env

# 3. 跑测试
uv run pytest
```

## 开发约定

- **TDD**：harness 层强制红-绿-重构（先写契约测试，再实现）。
- **单向依赖**：`app` 可 import `evaharness`，反向禁止（由 `tests/test_harness_boundary.py` 卡死）。
- **可插拔点**：统一用 `evaharness.reflection.resolve_class("module.path:ClassName")`，零 DI 容器。
- **配置**：三层（.env / config.yaml / extensions_config.json），启动加载 + `config_version` 校验，无热重载。

## 里程碑

- [x] M0 脚手架 + 边界测试 + 配置加载 + 反射加载
- [x] M1 模型工厂 + ReAct 执行器（LangChain `create_agent` + 6 个硬核中间件）
- [ ] M2 Plan-Exec 编排图（planner → subagent 并行派发 → 轻量 replanner → 汇总 + RunJournal trace）
- [ ] M3 gateway（内嵌 LangGraph Server）+ 前端（聊天 + 时间线）
- [ ] M4 工具（文件操作 + web_search/web_fetch + 纯函数）+ 四维度验收
