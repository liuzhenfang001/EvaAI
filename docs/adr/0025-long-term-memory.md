# 长期记忆：六块画像 + 原子事实（SQLite）+ 每轮隐藏消息注入

> **T21 修订**：本 ADR 的 §3（注入走 request-level override）、§4（提示词讲三件工具）、
> §5（三件工具都交给模型）与 §7（每轮注入）已被 [ADR-0026](0026-passive-memory-extraction.md)
> 取代：注入改为**会话开头写进 state 一次**、模型**只拿 `memory_search`**、写入改为**被动抽取**
> （采集中间件 + 去抖队列 + 水位线）。数据形状、SQLite 存储、预算裁剪与 REST 管理页仍然有效。

三期清单（#13）「相关现状」里那笔欠账：会话记忆与摘要压缩有（checkpointer + `SummarizationMiddleware`），
**跨会话的长期记忆没有**。本片（T20，issue #21）做**读侧 + 主动写侧**；被动抽取是 T21。

参照项目 DeerFlow 有一整套 `agents/memory/`（可插拔 `MemoryManager` 三层契约 + 后端目录
`deermem`/`mem0`/`noop`/`openviking` + 去抖队列 + LLM 抽取 + FTS5 检索 + 事实按 sha256 分片存 Markdown
+ 乐观锁 + 迁移 + 10 条 REST 路由 + 前端管理页 + 28 个测试文件）。本片**取它的形状、不取它的体量**。

## 决策

**1. 机制放 harness（`evaharness/memory`），策略在 app。**

与沙箱、知识库同一条分界（ADR-0020 / ADR-0022）。harness 里是通用的机器：数据形状、SQLite 存储、
渲染、三件工具；app 里是部署决定：库文件在哪、注入多少 token、哪些分类享保底通道。
`memory` 已加进 `tests/test_harness_boundary.py` 的 `MECHANISM_PACKAGES`——它不许 import
`evaharness.agents`。

**2. 记两样东西：六块画像（叙述）+ 原子事实（断言）。**

画像分两组——`user`（`work_context` / `personal_context` / `top_of_mind`）与 `history`
（`recent_months` / `earlier_context` / `long_term_background`）；事实带 `category`（九类）、
`confidence`、`status`、`source_thread`，按内容去重。

**`history` 那三块与 `SummarizationMiddleware` 不重叠**，这是两个不同维度的东西：后者压的是
**本 thread** 的上下文（会话内、按 token 触发、丢了就没了），前者是**跨会话**的长期历史记录
（用户换一个会话，它还在）。用户在本片明确要求按 DeerFlow 的形状保留这六块。

**3. 注入走 `wrap_model_call` 里的 `request.override`，不落 state。**

这是本片最重要的一条。写法照现成的
`TerminalResponseMiddleware._augment_request`：把记忆作为一条
`HumanMessage(content=<memory>…</memory>, name="memory", additional_kwargs={"hide_from_ui": True})`
附在**这一次模型请求**的最后。于是它：

- 不进 checkpointer（不会逐轮堆积成一份份历史快照）；
- 不进 RunJournal，也不漏进前端的对话投影（前端只看 journal 的 `message` 桶）；
- 被 `InputSanitizationMiddleware._is_genuine_user_message` 与
  `TerminalResponseMiddleware._tool_result_in_current_turn` 按 `hide_from_ui` 跳过。

**为什么是消息而不是 system prompt 的一段**（与技能 / bash / 知识库那三段的选择相反）：那三段是
**静态**的，拼一次用一辈子、前缀缓存友好；记忆**每轮会变**，拼进 system prompt 等于把每轮都变长的
东西塞进**指令位**——既抖动前缀，又把「模型自己写的数据」放到了指令该在的地方。放成一条 `role=user`
的隐藏消息，它在模型眼里与其它上下文成员同权，而不是一条新规则。

这与参照项目同一取舍（它的 `dynamic_context_middleware.py` 也插 `role=user` + `hide_from_ui`，理由是
OWASP LLM01）。**分歧在实现层**：DeerFlow 把它经 `before_agent` 写进 state，我们只在请求层 override
——所以我们的对话历史里一份记忆快照都不留。

**4. 提示词里那一段只讲「怎么用工具」，正文每轮注入。**

`MEMORY_SECTION` 是静态的，按「工具表里有没有 `memory_*`」门控（与 bash / 知识库同一个先例），
内容只有三句：值得长期记住的用 `memory_save`、不确定时 `memory_search` 查、`<memory>` 段是背景数据
不是指令。

**5. 三件工具只属于 lead agent。**

`_subagent_tools` 从「只剔 `present_files`」扩成「剔 `present_files` + `memory_*`」。理由与
`present_files` 同类：子 agent 的存在理由就是**上下文隔离**——它看不到父对话，也就没有判断「什么
值得长期记住」的上下文；而它自己的记忆注入也被摘了（只有 lead 链挂中间件），让它写而不让它读只会
写出一堆半截事实。参照项目同样只把记忆工具挂在 lead 上。

**6. 存储是 SQLite（`data/memory.sqlite`），同步 stdlib `sqlite3`。**

与 checkpoint / 指纹同一口径，理由照抄 `fingerprint/store.py`：动作都是小查询，引异步 I/O 只会让
每个调用点多一个 `await`。**代价照实记**：注入中间件每轮要在事件循环里同步读一次，量级是「一张小表
上的一条索引查询」。

**三张表**：

```sql
memory_profile(section PK, summary, updated_at)
memory_facts(seq INTEGER PK, id UNIQUE, content UNIQUE, category, confidence, status, source_thread, created_at, updated_at)
memory_watermarks(thread_id PK, last_message_id, updated_at)   -- T21 用；本片只建表与读写接口
```

`seq` 是自增序号而不是拿 `created_at` 排序：`created_at` 只到秒，同一秒记下的两条会打平，
打平后若按 id（= uuid）排，「新的在前」就成了假的。

**7. 注入的预算按优先级切，`correction` 享一条保底通道。**

`max_injection_tokens` 是整段预算。先切 `guaranteed_categories`（默认 `["correction"]`）那一份
（`guaranteed_token_budget`，默认 500），剩下给画像，再剩下给其余事实（按置信度降序）。**纠正是
「别再犯」的，置信度低不代表可以丢**——这是保底通道存在的全部理由。

**空快照返回空串**，中间件据此**完全不 override**（请求逐字节不变）；**装不下的一条整条丢掉**，
绝不截半句（半条断语的危害大过没有它）。token 数是**估**的（东亚宽字符算 1、其余算 1/4），
不引 tiktoken——它按模型下载 BPE、首次使用要联网，而 DeepSeek 又不在它认识的模型表里（ADR-0022 §7
在嵌入那边踩过同一件事）。

**8. 单用户部署，记忆全局一份，不引入「用户」维度。**

现有唯一坐标是 `session_id`（`sandbox/paths.py:5` 明写只做会话级隔离）。`source_thread` 只记
「这条是哪次会话产生的」，**不是分区键**。参照项目那套 `scope={userId, agentName}` 与
`strict_user_scope` 是为多用户写的，我们不需要。

## 硬约束

- **注入的 message 必须带 `hide_from_ui`**：否则 `InputSanitizationMiddleware` 会把它当成真实用户
  消息去包裹边界标记（`_is_genuine_user_message` 就是按这个标记排的）。
- **空记忆不 override**：没内容就直接返回原 request，否则所有开了 memory 的部署每轮都多一条空消息。
- **中间件只在 `memory.enabled` 时 append**：`test_lead_agent.py` 那三条断言比的是
  `build_default_middlewares()` + `[StepBudgetMiddleware]`（+ 摘要），默认链里不能多出东西。
- **记忆的记忆工具与提示词段只进 lead**：`_prompt(base, lead=...)` 因此有了那个参数——「提示词里
  提到一个模型调不到的工具」正是 bash / 知识库两段一直在避免的事。
- **`MemoryStore` 的 SQLite 连接有线程亲和性**：只许在**建连接的那个线程**（事件循环线程）上用，
  所以三件工具必须写成 `async def`——同步工具会被 langgraph 的 ToolNode 丢进工作线程。别把它们
  改成同步函数，也别把库的调用丢进 `asyncio.to_thread`。
- **`memory_facts.content` 的唯一索引是去重的唯一防线**：撞了抛 `DuplicateFact`，工具把它转成
  「Already remembered …」返回给模型（**预期之内的否定结果返回字符串，不抛异常**）。

## 后果

- lead 链在开了 memory 的部署里多一个中间件（默认关，没开的部署行为与从前逐字节相同）。
- 每轮多一次小 SQLite 查询与一段 ≤ `max_injection_tokens` 的注入；模型多三件工具。
- 新增一组 REST 路由（`/memory`）与一个前端页面（`/memory`）——这是「用户能自己抹掉模型记岔的
  东西」的唯一出口。
- `AgentFactory` 多一个构造参数 `memory`（一个 `Callable[[], str]`），与 `skills` 同一形状。

## 不做 / 范围外

- **被动抽取**（每轮结束后调 LLM 从对话里抽事实与更新画像、水位线推进、去抖队列）→ **T21**。
  本片把表与接口建好（`memory_watermarks` 的读写、画像的 `set_profile`），但没有生产者。
- **可插拔后端**（`MemoryManager` 契约 + `backends/<name>/` 目录 + `manager_class` 配置）：
  我们只有一个后端，为它造一层抽象是给一个不存在的替换写机制。
- **文本检索**（SQLite FTS5 + jieba 分词）、时间衰减打分、事实合并（consolidation）、staleness
  复核：那套是为 100 条事实上限与多用户写的；本片全量注入按预算裁剪，规模上限由 T21 的
  `max_facts` 封顶。
- **向量检索**：不碰 T18 的 Chroma / 嵌入栈。记忆与知识库是两条线——一个是「记住你」，一个是
  「查资料」。
- 子 agent 的记忆注入与跨 `task` 传递。

## 遗留债

- **画像目前没有生产者**：六块表建好了、渲染通了、管理页也能看，但只有 T21 的抽取器会写它；
  本片的 `memory_save` 只写事实。空画像在注入里整段消失（`render_memory_section` 先渲染画像，
  没有就只出事实那一块）。
- **`status` 列只有 `active`**：`superseded` 是为 T21 的合并留的位置，目前没有写入者。
- **`memory_search` 是 `LIKE` 子串匹配**：中文够用，但它不是语义检索——「我问过什么关于部署的事」
  这类查询要的是向量或 FTS5。事实规模小的时候这不是问题，上量之后是。
