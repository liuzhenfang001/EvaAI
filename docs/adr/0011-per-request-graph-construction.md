# 图与模型改在请求路径上构建（每请求建图）

T8 之前，lead agent / 子 agent / 模型 / Plan-Exec 的三个角色全都在 lifespan 里各建**一次**，
挂在 `app.state` 上被所有请求共享。这在并发下是错的，而且把图的结构在启动时冻结了。本 ADR
记录这次搬家：**建图从 lifespan 挪进请求路径**，`app.state` 只留长命的建图参数与共享物。

## 问题

**① 中间件实例字段 × 跨请求共享 = 并发污染。**

默认链七个中间件里只有两个带状态：`LoopDetectionMiddleware`（`_history` / `_warned` /
`_pending_warnings` / `_stop_reason` 四个 dict）与 `TerminalResponseMiddleware`（`_retried` /
`_pending_recovery` 两个 set）。它们按检查点坐标分桶，而**实例**是进程级的——于是同一个坐标
上的两个并发 run **共用一本账**。实测两条症状：

- 各只调两次同一个工具，账本合起来就到 `warn_threshold=3`，谁先走到下一次模型调用谁就收到
  一段 `[LOOP DETECTED]`（坐标不同时不会发生）。
- 后启动的 run 的 `before_agent` 会**清掉先启动那个的**重试预算。

可达路径很普通：前端把 `threadId` 放在 localStorage（同源共享），两个标签页读到同一个 id，
各发一条即可触发。服务端没有任何互斥或去重。

**② 图的结构在启动时冻结。**

模型参数（开启思考、温度、思考力度）、工具组装、提示词、中间件链都改不了。这不是单例的
副作用，是**定义性限制**——参照项目 DeerFlow 的 `make_lead_agent(config)` 就是在请求路径上
调用的工厂，从 `config["context"]` 读 `model_name` / `thinking_enabled` / `agent_name`，
图的形状本身依赖请求参数。

## 决策

**1. 主图 + 子 agent 图 + 模型全部每请求构建，三者同生命周期。**

不能只重建主图：`make_task_tool(subagent)` 把子图**闭包**进 `task` 工具，工具再被编进主图。
只重建主图的话，子图那批中间件实例仍然长命——子 agent 的桶继续无界增长，并发污染照旧。

**2. 唯一入口是业务层的 `AgentFactory`**（`app/gateway/agents.py`），方法 `lead()` 与
`plan_exec()`。它只持有**跨请求必须共享**的参数：配置、业务工具、checkpointer，**不持有任何
图或模型**。

封存的 Plan-Exec 一并改：同一个 app 里两套生命周期并存，读代码的人会以为那是有理由的设计。
它的三件套必须**一起换**——`make_react_executor` 在装配期就把图闭包进了 `execute`，只换
planner / replanner 等于没换。

工厂吃一个 `model_factory` 接缝（profile 名 → 模型，缺省走 `create_chat_model`）。它不只是
为了测试：**模型每请求新建**这条要求让「模型怎么来」必须可替换，而下一片「每请求参数从哪来」
要长出来的正是这个位置。

**3. `app.state` 只留长命物。**

`checkpointer` / `journal_config` / `journal_store` / `app_config` / `agent_factory`。
`AsyncSqliteSaver` 是**唯一**先天不可能每请求建的建图输入——它的 `__init__` 调
`asyncio.get_running_loop()`，只能在 lifespan 那个协程里构造（ADR-0007）。

与 issue 的一处偏差：issue 把 `business_tools` 也列进了 `app.state`，本片**没挂**——工厂已经
持有同一个 list，再挂一份是第三个引用，只会招来漂移。`BUSINESS_TOOLS` 仍是 `app.py` 的模块级
常量、唯一来源。

**4. 启动期 smoke build：装配一遍再丢掉，失败就拒绝启动。**

它保的是「启动期就该发现的错误不要拖到第一个用户请求」：模型 profile 的 `use` 指向不存在的
类（ReflectionError）、中间件链里塞了非中间件对象（AttributeError）、工具列表里塞了字符串
（AttributeError）、api key / base_url 缺失。失败**不降级成 warning**——默认路径都建不起来，
服务起来也没意义。

**它不是正确性证明。** 建图期**抓不到**的至少两类（实测）：工具重名（`create_agent` 静默
接受）、`state_schema` / `context_schema` 传错（同样不报错；`context_schema` 换错要等
`task` 工具派发那一刻才炸）。它抓的是「装配连线断了」，不是「语义接错了」。

**5. 刻意不做缓存。**

建图 3.10 ms、子图 2.20 ms、模型 0.39 ms——不值得省。而且按配置指纹缓存会**把共享的中间件
实例原样带回来**，且洞只在缓存命中时出现，比现在难复现一个量级。

**6. 刻意不做热加载。**

`load_config` 仍在 lifespan 里、启动时一次（loader 的模块 docstring 写死了这个契约）。图每请求
建之后，「改配置不重启」顺手就能做到，但那会带来「同一会话前后两个请求跑在不同配置上」的问题，
属于另一个设计。要改就得显式决定。

**7. 「该跨请求的状态」不许放中间件实例字段。**

今天没有这种需求（`TokenUsageMiddleware` 只打日志），所以本片代码零改动、只立规矩。但它必须
写下来：第一次有人往中间件里塞共享计数器（全局 token 预算、并发闸门），并发洞就会原样种回来。
要共享就走 `app.state` 上的显式对象注入，像 checkpointer 与 journal store 那样。

**业务工具同理**：它们跨请求共享，所以必须无状态——每请求建图不会复制工具对象。

## 实测依据

| 事实 | 数据 |
|---|---|
| 构建成本可忽略 | `make_lead_agent` 全套 3.10 ms、`make_subagent_graph` 2.20 ms、`create_chat_model` 0.39 ms |
| **重建不失忆** | 两次请求各建全新图 + 共享 checkpointer，第 2 轮仍看到第 1 轮的「我叫张三」——记忆在 checkpointer 里，不在图里 |
| `ChatOpenAI` 构造时不开 socket | 建出 `root_client` / `root_async_client`，已建连接数 = 0 |
| **重建模型不换连接池** | langchain-openai 按 `(base_url, timeout, socket_options)` 缓存默认 httpx 客户端；实测 `CacheInfo(hits=3, misses=1, currsize=1)` |
| 连接池会维持连接 | `keepalive_expiry = 5.0 s`、`max_connections = 1000` |

## 硬约束

- **`timeout` 不要做成每请求可配**：缓存键包含它，而 `httpx.Timeout` 不可哈希时会绕过缓存、
  每次新建客户端 → 池按请求泛滥，症状是「连接数悄悄涨」。缓存键**不含 `api_key`**，别指望它
  按身份隔离连接。
- **模型类必须能无连接构造**：把 profile 的 `use` 换成在构造函数里开连接/建线程池的实现，
  每请求构建就会每请求建资源。

## 后果

- **`_history` 不再无界增长**：实例随请求释放。ADR-0010 里「对 Plan-Exec 的坐标仍是无界增长」
  那条遗留债由此解除。
- **请求期建图失败 → 500 而不是 SSE error 帧**。旧结构下图在启动期建好，构建错误 = 服务起不来；
  新结构下语义错误会在 `StreamingResponse` **之前**炸，客户端拿到 JSON 500（前端期望 SSE，
  表现为「没有任何反应」，比 error 帧更难察觉）。smoke build 挡住配置/类路径/模型构造类错误；
  其余**刻意不包 try/except**——转成 error 帧会让「请求还没开始」这件事消失，反而错。
- **图的寿命 = 响应流的寿命**。`run_lead_agent` 是 async generator，图被它闭包住，`sse_consumer`
  迭代期间不会被回收；客户端断开 → 硬取消 → 图释放。别读成「请求结束就释放」。
- **`consume_stop_reason` 结构性失效**：实例随请求消失，app 层再也拿不到它。若将来要报「为什么
  被强停」，得改走 state 或事件流。本次顺带删掉了它旁边那条「写进 `runtime.context['stop_reason']`」
  的旁路——主路径传 `LeadContext`（永远进不去），而它进得去的时候是在**改写调用方给的对象**，
  正是本片要消灭的那类跨请求写。
- **`mode` 缺省值之类的旧取舍不受影响**（ADR-0010 第 4 条仍在）。

## 不做 / 范围外

- **不消除「同一会话并发写 checkpoint」的竞争**：两个标签页共用 `thread_id` 时，两个 run 依然写
  同一个 `{会话}:lead` thread。**今天就是这样**，本片不改变也不解决——别把它误读成「并发洞已全堵上」。
- **每请求参数从哪来**（请求体？按会话持久化？全局 config + 请求覆盖？）单独一片，需要先定契约。
- `events.py` 仍在 `agents/plan_exec/` 下（ADR-0010 已记为遗留债），本片不扩大范围。
- 前端无改动：本片没有用户可见面的变化。
