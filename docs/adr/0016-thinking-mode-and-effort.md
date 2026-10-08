# 思考模式与动态思考深度

DeepSeek 的思考模式（先出一段 ``reasoning_content`` 再出正文）在 EvaAI 里一直没生效，原因有
两层：配置用的 ``deepseek-chat`` 是个隐含「不带思考」的别名；而 ``langchain_openai.ChatOpenAI``
**三个落点都不处理 ``reasoning_content``**。本片把这条链打通，并让用户能在界面上选档位
（极速 / 思考·low·high·max），思考内容在气泡里实时可见。

## 实测结论（2026-10-08，本机 + 本项目账号）

这些数字是下面所有决策的依据，全部是**打真实 API 测出来的**，不是转述文档：

| 验的什么 | 结果 |
|---|---|
| ``GET /models`` | 只有 ``deepseek-flash``、``deepseek-v4-pro`` |
| ``deepseek-chat`` | 能用，但**是别名**——响应 ``"model":"deepseek-flash"``，且**默认不带思考** |
| ``ChatOpenAI`` 响应侧 | **不认**：``additional_kwargs`` 只有 ``{'refusal': None}`` |
| ``ChatOpenAI`` 请求侧 | **丢**：带 ``reasoning_content`` 的 ``AIMessage`` 序列化后只剩 ``{'content','role'}`` |
| ``reasoning_effort`` 生效吗 | **生效**。同一道题 ``low`` → 103 reasoning token、``max`` → 207 |
| 流式 delta 带 ``reasoning_content`` 吗 | **带**（216 个 chunk 里 170 个有） |
| langgraph 的 serde 保 ``additional_kwargs`` 吗 | **保**（checkpoint 往返后仍在） |
| 要发的形状（``extra_body.thinking`` + ``reasoning_effort``） | **可用**：极速 → 0 reasoning；思考 → 有 reasoning，且同一道题极速答错、思考答对 |
| 文档说「带 tools 时历史缺 ``reasoning_content`` 会 400」 | **实测不成立**：四种组合（带/不带 tools × 历史带/不带 reasoning）全部 200 |

## 决策

**1. 写一个 provider 专用子类，覆写三个方法。**

``langchain_openai`` 自己的模块 docstring 就写着：「Non-standard response fields added by
third-party providers (e.g., ``reasoning_content``) are **not** extracted or preserved. ...
**use the corresponding provider-specific package** instead」。我们没有那个包
（``langchain_deepseek``），所以按它建议的做：``evaharness/models/deepseek.py`` 的
``DeepSeekChatModel(ChatOpenAI)``。

丢的三个落点里有**两个是模块级函数**（``_convert_message_to_dict``、``_convert_delta_to_message_chunk``），
覆写不了，所以只能在调用它们的**方法**上补：

| 方向 | 覆写 | 不补的后果 |
|---|---|---|
| 请求侧回放 | ``_get_request_payload`` | 模型看不见自己上一轮的推理 |
| 响应侧·非流式 | ``_create_chat_result`` | 思考整段丢失 |
| 响应侧·流式 | ``_convert_chunk_to_generation_chunk`` | **思考只能等整段说完才出现**，而它最大的价值是「边说边想」 |

请求侧的匹配**按下标 zip** 而不是照抄参照项目的签名匹配：``_get_request_payload`` 里
``payload["messages"]`` 是对同一个 ``messages`` 列表做的列表推导，条数与顺序都不可能变
（本机源码 L2041-2046 核对过）。**加了长度相等的守卫**：不等就整段跳过——宁可丢字段，也不要
把某一轮的思考贴到另一轮上，那种错配是静默的。

**2. 请求侧回放照做，但理由不是「不回放就崩」。**

DeepSeek 文档说带 ``tools`` 时历史必须回传 ``reasoning_content``，否则 400。**实测不 400**
（见上表）。仍然回放，因为：那是文档写明的契约，不该依赖服务端当下的宽容；而且不回放模型就
看不见自己之前的推理。代价是思考会占上下文 token——接受。

**3. 思考开关的翻译放工厂，不散到调用方。**

``extra_body`` 在 ``create_chat_model`` 里是**整键替换**的：调用方各自去拼，会把 profile 里
原有的 ``extra_body``（厂商的其它参数）整个冲掉，而且**不报错**——症状是「一开思考，之前配的
参数就没了」。所以 :func:`~evaharness.models.factory.thinking_model_kwargs` 统一产形状，
:func:`~evaharness.models.factory._merge_extra_body` 按层合并。有一条测试专门钉这个。

副作用：**给未来的每请求旋钮一个家**，而不是让它们一个个变成新形参。

**4. `RunOptions` 就是 ADR-0011 欠的那份契约。**

[ADR-0011](0011-per-request-graph-construction.md) 把「每请求参数从哪来」明确留给后续切片，
并指名参照项目的 ``make_lead_agent(config)`` 读 ``thinking_enabled`` 为范例。本片落地为
``evaharness/agents/options.py`` 的 ``RunOptions``，``ModelFactory`` 缝随之从
``Callable[[str|None], …]`` 扩成 ``Callable[[str|None, RunOptions|None], …]``。

**硬约束照旧**：放进去的只能是「出现在请求 JSON 里」的东西。``timeout`` / ``http_client``
参与 httpx 客户端缓存键，每请求变会让连接池按请求泛滥——**不许进来**（ADR-0011）。

**5. 事件回显档位，缺省是 `off`。**

``RunStartEvent`` 加 ``thinking``，缺省 ``"off"``。这个缺省是**历史回退**而不是语义缺省：老
journal 行里没有这个字段，而那些运行确实没有思考。**特意与请求层的默认档（``high``）不一致**
——两边各自的缺省都对着自己那一侧的事实；跟着请求层走会让旧记录凭空多出一段并不存在的思考，
而回放的全部意义就是「与实时逐字一致」（与 ``mode`` 缺省 ``plan`` 同一个手法）。

**6. 默认档是「思考·high」。**

与 DeepSeek 自己的默认对齐（thinking 默认开、effort 默认 high）。用户选的档位记在
``localStorage``（``core/chat/preferences.ts``），**不进 reducer**——偏好是用户的选择、不随运行
变化，塞进 reducer 会污染回放（同一条 journal 在别人机器上会渲染出不同的东西）。

**7. 模型名换成 `deepseek-flash`。**

``deepseek-chat`` 现在只是个别名，且**别名自带了「不带思考」的语义**，与我们要发的 ``thinking``
参数语义重叠——留着它就是在两处同时控制同一件事。用真实模型名 + 显式开关。

## 后果

- **配置换了实现类**：``use: evaharness.models.deepseek:DeepSeekChatModel``。这是照既有的可插拔
  机制走的（``use`` 指向 ``module.path:ClassName``），harness 不因此多知道任何业务。
- **前端多一种步骤**：思考与正文是**两条并行的流**，各自成为**独立的一步**（「第 N 步 · 思考」/
  「第 N 步 · 调用 X」），共用既有的折叠与实时打字那一套。做法是给轮次加一个
  `kind: "thinking" | "answer"`，而不是在轮次里塞一个 `reasoning` 字段——合成一步再在内部拆的话，
  界面上「第 N 步」的编号会与实际发生的事对不上（想一次、说一次、调一次工具，是三步）。
- **`finalText` / `processRounds` 因此必须判 `kind === "answer"`**：模型**只想了没来得及说**
  （截断 / 超时 / 取消）时，最后一步是 `thinking`——少了这个判断，那段内心独白会被当成对用户的
  答复显示出来。
- **子 agent 与 Plan-Exec 跟随同一份运行参数**：建图时一个模型实例同时喂给主图与子图，整份
  options 一起走。**摘要模型不吃运行参数**——压缩是内部辅助调用，让它跟着思考档位跑只会让每次
  压上下文都变成一次昂贵思考。

## 已知行为 / 边界

- **`reasoning_effort` 是提示不是硬上限**：简单题上 ``low`` 与 ``max`` 的思考量可能几乎一样
  （实测 59 vs 53），差异在难题上才明显（103 vs 207）。**验收要用难题**，否则会误判成「档位没生效」。
- **思考模式下 provider 忽略 `temperature`**（官方说明）。配置里的 ``temperature`` 是留给
  「极速」档的。
- **`with_structured_output`（planner / replanner）那条路保不住思考**：它走 ``response_format``
  时流式由 ``beta.chat.completions.stream`` 供给，最终 completion 不经我们覆写的方法。对我们不
  致命（规划过程不需要展示思考），但要知道「不是全路径都保住了」。
- **`reasoning_effort` 的取值卡死在 `low|high|max`**：SDK 允许更宽的一套
  （``none/minimal/medium/xhigh``），但 provider 只认这三个——传别的换回 400。``off`` 档下我们
  发 ``minimal``（实测 200 且完全不产 reasoning）。
- **请求侧回放只对 V4 系模型成立**。契约在代际之间**反过来了**：R1 系（``deepseek-reasoner``）
  要求把 ``reasoning_content`` **删掉**再发，带上它反而 400。我们现在用的是 V4 系
  （``deepseek-flash``），回放是对的；**换成 R1 系模型之前必须先关掉它**。
  这也顺带解释了「为什么 ``langchain_deepseek`` 不做请求侧回放」——它面向 R1 那一代，
  而父类「丢掉非标准字段」恰好就是那一代要的「删掉」，**不是遗漏，是对**。

## 不做 / 范围外

- **模型选择器**（``deepseek-flash`` / ``deepseek-v4-pro`` 切换）：本片只做思考开关与深度。
- **思考内容参与摘要压缩的取舍**：思考照常进 journal（可回放优先），体积问题与
  ``MessagesEvent.metadata`` 当初的决定同一立场（可逆的那一边）。
- **三期**：docker 沙箱、skill 加载机制、MCP、Rag（含将来的 llm wiki）。
