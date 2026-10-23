# 长期记忆二期：被动抽取 + 会话开头注入（T21）

T20（ADR-0025）交付了长期记忆的读侧与**主动写侧**：三件工具 + 每次模型调用重放一遍注入。
验收不通过，四个问题都出在「写入靠模型、注入靠请求层」这个选择上：

1. 模型要走一步判断「该不该记」并调 `memory_save`，污染主线任务，还会在任务面板留下工具调用；
2. 画像表建好了却**没有生产者**；
3. 用户能感知到「助手在操作记忆」（工具气泡 + 「好的，我记住了」）；
4. 注入不落 state，所以每个请求、每个 ReAct 步骤都要重放；对话历史里又看不到它。

本 ADR 记录 T21 的三处反转。**与参照项目 DeerFlow 同形状、不同实现**的地方写在每节里。

## 决策

**1. 注入从「请求层 override」改成「会话开头写进 state」，thread 内只写一次。**

`MemoryMiddleware` 现在在 `before_agent` 里返回一条 `hide_from_ui` 的 `HumanMessage`，由
`add_messages` 合并进 state。之后每轮模型都从历史里看到它，不再重放。

**首轮判定用 marker，不用「有没有 AIMessage」**：空库时第一轮没有可注入内容；如果用「已经有
AI 消息」当判据，这个会话就永远看不到后来积累的记忆。改成「state 里没有记忆 marker 且
`render()` 非空」后，注入发生在**第一次有记忆的那一轮**，此后不再重复。

**摘要是唯一会让它再出现一次的路径**：`SummarizationMiddleware` 会把老消息整段删掉，marker
随之消失，下一轮补注一次。这是修复（模型确实已经看不到它了），不是重复注入。严格的「thread
内一次」与「压缩后记忆不丢」只能二选一，我们选后者。

**确定性 id**（`memory-injection:<会话>`）：同一会话并发两个 run 时，`add_messages` 按 id 是
替换而不是追加，最多留一条。

与 DeerFlow 的分歧：它的 `dynamic_context_middleware` 也插 `role=user` + `hide_from_ui`，
但经 `before_agent` 写进 state **并且每轮都插**；我们只在**没有 marker 时**插一次。

**2. 写入完全被动：采集中间件 → 去抖队列 → 抽取器 → 水位线。**

- `MemoryCaptureMiddleware.after_agent` 把 state 交给 app 层的 capture 闭包；
- capture 按**水位线之后**的窗口截出完整轮次（真实用户消息 + 最终助手回复，排除 Tool 消息、
  带 `tool_calls` 的 AI 消息、`hide_from_ui` 注入、兜底错误文案），寒暄与过短消息丢弃；
- `MemoryQueue` 按会话去抖（默认 30 秒）、单 worker 串行；每 thread 只留**最新一份** capture
  （窗口是旧窗口的超集）；
- `MemoryUpdater` 再读一次水位线、调模型、落库、推进水位线。

**按 watermark 扫整段窗口，而不是只取最后一轮**：抽取失败不推进水位线，下一次 capture 必须
能把失败那一轮带上；关机丢弃未决项同理（丢弃的是「这次没做」，不是「永远不做」）。watermark
是**消息身份**不是有序游标——在 state 里按 id 定位下标；找不到（被摘要删掉）只回退到最后一个
完整轮次，不整段重扫。

**不做 DeerFlow 的信号检测**（correction / preference 的正则或规则打分）：它的作用是把抽取
引导到某类信号，代价是多一层会误判的规则。我们的抽取 prompt 已经直接列了该记与不该记的类别，
用一次完整的对话窗口比「先判信号再抽」更简单，也少一个需要维护的分类器。

**3. 工具面收敛：三件工具保留在代码里，模型只拿 `memory_search`。**

`LEAD_MEMORY_TOOL_NAMES = {"memory_search"}` 显式过滤。save / forget 不进模型工具表——写入
已经被动化，让模型再主动存一次只会重复、且重新引入「可感知的操作」。`memory_search` 增加
`since`（ISO 8601，按 `updated_at` 过滤），允许空 query + 时间，用来回答「最近更新了什么」。

**4. 抽取走 `json_mode`，不走 `with_structured_output` 的默认路径。**

`DeepSeekChatModel` 继承 `ChatOpenAI`，而 `with_structured_output` 默认签发
`response_format={"type": "json_schema"}`；DeepSeek 的 Chat Completions 只认
`{"type": "json_object"}`，默认路径会 400。因此显式 `method="json_mode"` + schema 写进 prompt
+ `tolerant_parse_update`（去围栏、抠首尾大括号）+ 一次重试。抽取固定 `thinking="off"`：后台
整理跟着用户的思考档位跑没有意义。

**5. 冲突处理是「追加 + 替换 + 删除」。**

抽取 prompt 带上现有事实的 id（最多 `max_facts_in_prompt` 条），模型可以 `facts_update` /
`facts_remove`。只追加的话，同一件事说两遍就会留下两条互相矛盾的记录，注入里越积越乱。
落库顺序 **remove → update → add → profile**：`content` 唯一索引要求先删释放名字，先改再增
才不会被「改名」撞上自己。每条单独 try，坏条目跳过而不是整批作废。

**6. 队列的 epoch 给「清空全部」用。**

`DELETE /memory` 会在抽取飞行途中清库。队列递增 epoch 并清空 pending；飞行中的 updater 在
落库前再问一次 `is_stale()`，过期就丢弃——否则用户刚抹掉的东西会在几秒后被写回来。

**7. 关机丢弃未决项，不 flush、不推进水位线。**

flush 会让关机等一次 LLM 调用；推进水位线则等于承认这些对话永远不抽。两者都不选：丢弃的是
「这次没做」，下次用户回到这个会话说话，capture 会从旧水位线把轮次一起带上。

## 硬约束

- **注入消息必须带 `hide_from_ui`**：`InputSanitizationMiddleware` 与 `TerminalResponseMiddleware`
  都按它跳过。
- **journal 不泄漏不能只靠 `trace=False`**：`create_agent` 的中间件节点当前不触发
  `on_chain_end`，所以 `before_agent` 的返回值不进 `messages` 流；但那是内部实现。事件翻译层
  额外按 `hide_from_ui` 挡一次，并用端到端测试钉住「journal 里没有 `<memory>`」。
- **SQLite 线程亲和性**：capture / updater / 工具全在事件循环线程；`apply_update` 同步执行，
  不丢进 `to_thread`。
- **抽取失败不推进水位线**，连续失败 `max_failures` 次后主动放弃并记 error，避免永久卡在同一段。
- **测试不靠 sleep 造时序**：本机 Windows 上亚 16ms 的 `asyncio.sleep` 会提前触发；队列测试
  用 `flush()` 或 `debounce_seconds=0` + `Event`。

## 后果

- 每个会话的模型请求里多一条隐藏消息（一次），不再每轮回放；提示词里的「怎么用记忆工具」只讲
  `memory_search`。
- 会话中途抽取出的新记忆要**下一个会话**才进上下文；当前会话靠 `memory_search` 兜底。这是
  「只注入一次」的直接代价。
- 后台多一个队列 task 与一次抽取 LLM 调用（安静 30 秒后触发）；关机时未决项丢弃。
- `/memory` 管理页仍然可用；画像从 T21 起才会真正有内容。

## 不做

- 信号检测（DeerFlow 的正则打分）。
- 可插拔后端（`MemoryManager` + `backends/`）、FTS5 / jieba、向量检索、时间衰减。
- 同一会话并发 run 的 checkpoint 串行化（既有问题，不归记忆层解决；确定性 id 只保证注入不重复）。
- 关机 flush。
