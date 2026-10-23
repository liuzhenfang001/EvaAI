# EvaAI

企业级可用标准的**个人 AI 助手**。顶层是一个 **ReAct 主 Agent**（`lead agent`）——用户直接与它
对话，它自己决定调什么工具、要不要派子 agent。

核心是 **harness 工程思想**：框架层（harness）与业务层（app）分离，harness 不关注业务、只为业务
提供可扩展性支持。整体层级与框架严格参照本地参考项目 DeerFlow，但针对「个人助手、本地部署」
大幅裁剪。

## 目录结构

```
backend/
├── packages/harness/evaharness/   # 框架层（业务无关，可插拔机制）
│   ├── runtime/                   # 契约中枢：事件契约 / 运行上下文 / 运行入口与取消
│   │   ├── event/                 #   RunEvent 契约 + 工具载荷 + 图流翻译
│   │   ├── context/               #   RunContext + 访问器 + 每请求旋钮
│   │   └── runs/                  #   run_agent + pump + 取消 + 终态读取
│   ├── agents/                    # agent 层：ReAct 图 + 子 agent 派发 + 治理中间件
│   ├── sandbox/                   # 虚拟路径沙箱 + docker 沙箱 + 文件工具 + bash
│   ├── knowledge/                 # RAG 知识库：切分 / 向量存储 / 索引器 / 检索工具
│   ├── memory/                    # 长期记忆：画像 + 事实（SQLite）+ 注入 / 采集 / 抽取
│   ├── fingerprint/               # 目录指纹与变更检测
│   ├── checkpoint/                # checkpoint 持久化（SQLite）与保留策略
│   └── journal/                   # RunJournal + 可插拔 store（内存 / JSONL）
├── app/                           # 业务层（助手人设、具体工具/技能）
│   ├── gateway/                   # FastAPI + SSE + 每请求建图的 AgentFactory
│   └── tools/                     # 工具注册表 + 沙箱布局 + 交付/联网工具
└── tests/                         # 测试（含 test_harness_boundary.py 强制单向依赖）
frontend/                          # Next.js 前端（会话栏 + 聊天区 + 浮动任务面板 + /memory 管理页）
skills/                            # 技能源（启动期镜像到 /skills，见 skills/README.md）
knowledge/                         # 知识库源（启动期镜像到 /knowledge 并建向量索引）
config.yaml / config.example.yaml  # 主配置（版本化，$ENV 引用）
extensions_config.json             # 扩展（MCP / skills / middlewares），模板同上
.env                               # 密钥
data/                              # 运行时数据（checkpoint + 记忆库 + 向量库 + 指纹 + journal）
backend/.evaai/                    # 文件沙箱运行期数据（input/output/workspace/...，已 gitignore）
```

## 环境要求

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)（依赖与虚拟环境管理）
- Node.js >= 20 与 npm（前端）
- [Docker Desktop](https://www.docker.com/products/docker-desktop/)**（可选）**——只有要用 `bash`
  工具（docker 沙箱）才需要；不装的话服务照常起

## 快速开始

```bash
# 1. 安装依赖（backend/.venv）
cd backend
uv sync

# 2. 准备配置
cp ../config.example.yaml ../config.yaml
cp ../.env.example ../.env        # 填入 DEEPSEEK_API_KEY；要用知识库再填 DASHSCOPE_API_KEY
cp ../extensions_config.example.json ../extensions_config.json

# 3. 跑测试
uv run pytest

# 4. 启动后端（端口 8010）
uv run uvicorn app.gateway.app:app --port 8010

# 5. 启动前端（另开终端，端口 3010）
cd ../frontend && npm install && npm run dev
```

浏览器打开 <http://localhost:3010> 即可对话。左侧会话栏支持新建、切换、重命名与删除；对话与运行
记录落在 `data/journal/<会话>/`，刷新页面或重启后端后会重建。

### 配置与端口

- **配置**：三层（`.env` / `config.yaml` / `extensions_config.json`），启动时加载并校验
  `config_version`，不支持热重载。
- **端口**：后端 8010、前端 3010。前端访问后端的地址只写在
  [frontend/src/core/api/config.ts](frontend/src/core/api/config.ts) 一处；要换地址就在
  `frontend/.env.local` 里设 `NEXT_PUBLIC_API_URL`（Next.js 只从前端目录读环境文件，仓库根的
  `.env` 是给后端用的）。
- **联网工具**：两个 key 都是可选的。`web_search` 走 Tavily，不填 `TAVILY_API_KEY` 就用无 key
  模式（能用，有速率上限）；`web_fetch` 走 Jina Reader，不填 `JINA_API_KEY` 同样只是速率受限。

## 开发约定

- **单向依赖**：`app` 可 import `evaharness`，反向禁止（由 `tests/test_harness_boundary.py` 卡死）。
  见 [ADR-0001](docs/adr/0001-harness-app-one-way-dependency.md)。
- **可插拔点**：统一用 `evaharness.reflection` 的 `resolve_class`（类）/ `resolve_variable`
  （实例，如工具），配置里写 `"module.path:attr"`，没有 DI 容器。
- **测试**：`uv run pytest` 内置覆盖率门槛（`--cov=evaharness --cov-fail-under=80`）；要看缺哪几行，
  单跑 `uv run pytest --cov-report=term-missing`。harness 层按红-绿-重构先写契约测试。
- **日志**：`config.yaml` 的 `log_level` 管 `app.*` / `evaharness.*` 两个命名空间，启动期消费
  （[ADR-0023](docs/adr/0023-log-level-consumption.md)）；uvicorn 自己的日志另有 `--log-level`。
  第三方库（httpx / chromadb）的日志不受我们管辖。

## 工具集

agent 能做什么由 `config.yaml` 的 `tools:` 决定（见 [ADR-0012](docs/adr/0012-tool-registry-and-file-sandbox.md)）。
**加一个工具 = 写一个 `@tool` + 加一项配置，不改框架代码**：

```yaml
tools:
  - name: my_tool
    use: app.tools.mine:my_tool        # 也可以指向工厂：app.tools.web:make_web_search
```

`use` 指向工厂时，同一项里的其它键就是工厂的关键字参数（在启动期绑进闭包，模型看不到也改不了）。
`name` 必须与工具自己的 `.name` 一致；全表不许重名、不许占用 `task`——这几条在启动期校验，写错就
拒绝启动。

工具看到的路径是**虚拟路径**，宿主目录藏在 `backend/.evaai/` 后面：

| 虚拟 | 用途 | 权限 |
|---|---|---|
| `/workspace` | 工作台，相对路径的落点 | 读写 |
| `/output` | 交付给用户的成品 | 读写 |
| `/input` | 用户上传的原件 | 只读 |
| `/skills` | 技能说明文件（启动期从仓库的 `skills/` 镜像过来） | 只读 |
| `/knowledge` | 知识库原文（启动期从仓库的 `knowledge/` 镜像过来） | 只读 |

## MCP

在 `extensions_config.json` 的 `mcpServers` 里声明一个 server，它的工具就会出现在 agent 的工具表里：

```json
{
  "mcpServers": {
    "fs": {
      "command": "npx.cmd",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:/somewhere"]
    },
    "remote": {
      "url": "https://example.com/mcp",
      "headers": { "Authorization": "Bearer ..." }
    }
  }
}
```

`transport` 可以不写——有 `command` 就是 `stdio`，有 `url` 就是 `http`（只支持这两种）。工具名一律是
`mcp__<server>__<tool>`，所以不会和 `config.yaml` 里的业务工具撞名。

**临时不用某个 server**：给它加 `"enabled": false`（缺省开）。关掉的 server 根本不建连——不 spawn
子进程、不占启动时间。配置留着，想用时改回 `true` 重启即可。改完要**重启后端**（工具清单是启动期
常量，见 [ADR-0017](docs/adr/0017-mcp-connections.md)）。

几条要记住的行为：

- **工具清单在启动期取一次**，之后每次运行零 I/O；连接建在模型真的调某个工具的那一刻。不用 MCP
  的对话完全免费；代价是「调几次就付几次建连」，连不上的 server 要重启才生效。
- **一个 server 连不上不影响别的**，也不影响服务启动；启动期会连一次并响亮地警告。
- **Windows 上要写 `npx.cmd`，不能写 `npx`**——PATH 里那个无扩展名的 `npx` 是 shell 脚本，
  `CreateProcess` 会报 `WinError 193`。（命令解析到无扩展名文件时会自动找同名 `.cmd`。）
- 没配任何 server 时，这套代码不会被执行。

## 技能

`skills/<名>/SKILL.md` 里写一段「做事方法」，重启后端后它就会出现在模型的提示词里：

```markdown
---
name: 写周报
description: 把这一周做过的事整理成一份能直接发出去的周报。
---

正文：给模型看的操作步骤。
```

规则只有两条硬性的：**`name` 必须与目录名逐字相同**、**`description` 必须有**（它直接进提示词，
模型只凭这一句决定要不要读正文）。其余字段会被忽略。完整说明见 [skills/README.md](skills/README.md)。

几条要记住的行为：

- **正文不进提示词**：提示词里只有名字、描述和一个位置，模型判断相关时自己 `read_file` 读全文。
  这样提示词不会随技能正文膨胀，改一个技能也不会让前缀缓存失效。
- **启动期镜像**：`skills/` 是源（进版本控制），`backend/.evaai/skills-view/` 是视图（运行期数据）。
  源里删掉的技能也会从视图里消失；一个技能都没有时不建视图目录。
- **改技能要重启**（没有热加载）。
- **坏技能只警告**：启动日志里点名是哪个目录、差在哪里，服务照常起。
- 想停用某个技能就把它的目录移走。

## docker 沙箱

给模型一个能跑命令的 Linux 容器。**默认关闭**——`config.example.yaml` 里 bash 那一条是注释掉的，
要用得先装 [Docker Desktop](https://www.docker.com/products/docker-desktop/) 并准备好镜像：

```bash
docker pull enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0
```

然后把模板里 bash 那一段的注释去掉（镜像、网络、超时、内存、CPU、进程数都是它的旋钮）。见
[ADR-0019](docs/adr/0019-docker-sandbox.md)。

**它是怎么和文件工具对上号的**：容器里那些目录就是虚拟路径本身（`/workspace`、`/output`、
`/input`、`/skills`、`/knowledge`），宿主目录经 bind mount 映射进去。所以容器里
`echo hi > /workspace/a.txt`，文件工具 `read_file /workspace/a.txt` 立刻读得到——是同一个文件。
只读目录的只读由 docker 强制。

几条要记住的行为：

- **每会话一个长驻容器**，名字 `evaai-<会话 id>`；装过的包在会话里留存。删除会话会连带删掉它，
  后端启动时也会清掉孤儿容器。
- **挂载目录之外的东西会丢**（`/tmp`、装的包、后台进程）；要留下的写 `/workspace` 或 `/output`。
- **每次 `bash` 调用是全新 shell**：`cd`、`export` 不跨调用。
- **命令的非零退出码是正常返回**（形如 `[exit code 1]`），只有 docker 这一层失败才是工具错误。
- 启用了 bash 而 docker / 镜像不可用 → 服务拒绝启动，并在日志里给出可执行的提示。

## 知识库

给模型一个可检索的本地知识库。源是仓库根的 `knowledge/`（把文档放进去即可，`.md` / `.txt`），启动期
切分、嵌入、存进本地 ChromaDB，模型用 `knowledge_search` 工具按需取用。

**默认关闭**——`config.example.yaml` 里 `knowledge` 那一段是注释掉的，要用得先有嵌入 key（`.env`
里填 `DASHSCOPE_API_KEY`，走通义 `text-embedding-v4` 的 OpenAI 兼容端点），再把 `enabled` 打开。
缺 key 却 `enabled: true` 会拒绝启动。细节见 [ADR-0022](docs/adr/0022-knowledge-base-rag.md)。

- **源目录即真相**：改 `knowledge/` 里的文件后重启，启动期比对按文件的指纹，只对改动过的文件重切、
  重嵌、替换。没变就跳过。
- **换嵌入模型会整库重建**：模型 / 端点 / 维度 / 切分参数构成「索引配方」，记在向量库里；不一致就
  丢弃旧向量全部重算。
- **重建在后台跑，不拖慢启动**；重启后第一条检索会等索引就绪，构建失败则那次检索可见地报错。
- **命中不塞回上下文**：`knowledge_search` 把匹配片段写进 `/workspace/retrieved/<hash>/chunk_i.md`，
  只回路径，模型自己 `read_file`。
- **`/knowledge` 是只读挂载**：模型命中片段后还能读原件全文；它随 bash 容器一起只读挂载。
- **中文友好**：切分显式带中文句末标点，不靠默认的英文分隔符硬切。

## 长期记忆

让助手**跨会话**记住关于你的事：六块画像摘要（工作 / 个人 / 当前关注 + 最近几个月 / 更早的上下文 /
长期背景）与一条条原子事实，存在本地 `data/memory.sqlite`。

**默认关闭**（`config.example.yaml` 里 `memory` 那一段是注释掉的）。打开它不需要任何 key，只需把
`enabled` 改成 `true`。细节见 [ADR-0025](docs/adr/0025-long-term-memory.md) 与
[ADR-0026](docs/adr/0026-passive-memory-extraction.md)。

- **写入是被动的**：每轮对话结束后，系统只把「你的消息 + 助手的最终回复」放进一个按会话去抖的后台
  队列（默认安静 30 秒），由抽取模型整理成画像与事实。模型没有 `memory_save` 可调，所以不会在任务
  面板里看到「助手正在记东西」，也不会听到它说「好的，我记住了」。同一件事说两遍会被合并/替换，
  而不是留下两条矛盾记录。
- **注入在会话开头一次**：当前记忆渲染成一段 `<memory>…</memory>`，作为一条 `hide_from_ui` 的隐藏
  消息写进会话状态；之后每轮模型都能从历史里看到它，但不会再重复。它不进 RunJournal、也不出现在
  前端。会话中途新抽取的记忆从下一个会话生效；当前会话需要时由模型用 `memory_search` 查。
- **模型只拿 `memory_search`**：可按关键词、分类或起始时间（`since`，ISO 8601）检索事实。
- **有预算**：整段注入默认 2000 token；`correction`（你纠正过的事）先占 500 token 的保底通道，剩下
  的给画像、再给其余事实。装不下的一条整条丢掉，不截半句。
- **只属于主 Agent**：记忆工具与注入都不进子 agent（与 `present_files` 同规则）。
- **能自己改**：`/memory` 页面可以看、增、改、删每一条事实，也能一键清空。

## 文档

- [CONTEXT.md](CONTEXT.md) —— 项目词汇表（术语与用词约定）。
- [docs/adr/](docs/adr/) —— 架构决策记录。
