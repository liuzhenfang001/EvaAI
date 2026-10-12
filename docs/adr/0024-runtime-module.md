# 运行期收进 evaharness/runtime：契约下沉、运行入口泛化（T19）

到 T18 为止，「跑一次 agent」这件事**没有自己的家**：契约与生命周期散在四处，并因此长出三条
不该有的跨包依赖边。本 ADR 记录把它们收成 `evaharness/runtime` 的决策，以及**不照搬参照项目**
的那一半。

## 背景：三条边与一个被埋掉的契约

| 证据（T19 之前） | 问题 |
|---|---|
| `sandbox/bash.py:27`、`sandbox/file_ops.py:23`、`knowledge/tool.py:24`、`app/tools/present_files.py:32` 都 import `evaharness.agents.middlewares.context` | 工具包依赖**具体 agent 实现包**的内部命名空间——「写一个工具」等于「认识 agent 实现」 |
| `agents/lead_agent/agent.py:26`、`runner.py:59` import `evaharness.sandbox.paths`，而 `sandbox → agents` 反向也有边 | **包级循环**，靠 `context.py` 的 `TYPE_CHECKING` 续命（ADR-0020「硬约束」段自认） |
| `journal/{journal,stores,compaction}.py` import `agents.events`；`models/factory.py:16` import `agents.options` | 通用设施被迫认识 `agents`：journal 只是想在事件流上挂个 tee，models 只是想要一个「思考档位」的枚举 |
| `RunCancellation`（harness，`runner.py:64`）与 `RunRegistry`（app，`gateway/run_registry.py`）分居两层 | 同一条取消协议的两半不在一起，靠 `chat.py:_make_cancellation` 的 lambda 缝合 |

还有一处**名不副实**：`agents/middlewares/context.py` 叫「middlewares」，但它的消费者大半不是
中间件，而是**工具**（`sandbox` / `knowledge` / `present_files`）。名字把工具引向了 `agents` 包。

## 决策

**1. 新建 `evaharness/runtime`，按三个概念分三个子包。**

```
evaharness/runtime/
  __init__.py        门面：再导出全部公开名（对外只认这条路径）
  event/             一次运行**说什么**
    contract.py        RunEvent 判别联合（含 Plan-Exec 的只读遗留契约）
    custom.py          工具经 custom 流送载荷时两侧共用的契约
    translate.py       图流（messages / values / custom）→ RunEvent
  context/           这一轮**是谁**
    contract.py        RunContext
    accessors.py       session_id / thread_bucket / sandbox_layout
    options.py         RunOptions / ThinkingLevel
  runs/              怎么**跑起来**
    run.py             run_agent（唯一入口）
    pump.py            内层流 → 队列，三种结束各归一类哨兵
    cancellation.py    RunCancellation + RunRegistry
    state.py           final_text + pending_interrupt
```

**对外门面原则**：一律 `from evaharness.runtime import X`，不写子包路径。子包只服务于「读代码时
一眼看出这是哪一类东西」，不是 API 面——照子包路径 import 等于把内部结构焊死。

**2. 依赖方向：`runtime` 是 L1/L3，机制包（L2）只许向下依赖它，不许 import `agents`（L4）。**

新加一条守卫测试（`tests/test_harness_boundary.py::test_mechanism_packages_do_not_import_agents`）
把这条方向钉死。它替代的是原先那种「靠注释约定 + `TYPE_CHECKING`」的守法——破例的症状不是崩溃
（Python 扛得住包级环，T19 之前就是这么活的），而是**包不再能单独存在**。

**3. 运行入口泛化：`run_lead_agent` → `run_agent`，`LeadContext` → `RunContext`。**

`lane`（`"main"`）与 `mode`（`"react"`）从函数体里的硬编码变成**参数**，`LEAD_LANE` 改名
`DEFAULT_LANE` 并落到 `event/contract.py`。理由不是洁癖：`run_agent` 原先要 `from
evaharness.agents.lead_agent.agent import LEAD_LANE`——**不参数化就搬不走**，搬进 runtime 会
立刻造出一条 `runtime → agents` 的反向边，正好抵消这次搬家的全部收益。

语义上也对：子 agent 复用同一个上下文类型（只是坐标不同），所以它本来就不是「lead 的上下文」；
`run_agent` 只吃一张编译好的图，本来就不该知道是谁在跑。

**4. `RunRegistry` 从 app 搬进 `runs/cancellation.py`，与 `RunCancellation` 同处。**

它是纯 `asyncio` 的 dict、零业务知识，本来就住错了层。分层的缝仍然保留：**接缝**（`RunCancellation`，
一次运行怎么交出把手）与**登记表**（`RunRegistry`，缺省实现）是两个东西，将来若真要跨 worker，
换掉的是后者。

**5. 事件契约整体搬移，遗留类一个不改。**

`PlanEvent` / `ReplanEvent` / `TaskStartEvent` / `SummaryEvent` 及它们的载荷类型（`PlanTask` /
`TaskResult` / `TerminalTaskStatus`）是**只读的遗留契约**——新运行不再产生，只为解析老 journal 而
存在。搬家时一个字段没动，回放实测通过（三个含 `plan` 事件的真实老会话照常解析）。

## 与参照项目的对照

| 处 | DeerFlow | EvaAI |
|---|---|---|
| 有没有 `runtime` 模块 | 有：`backend/packages/harness/deerflow/runtime/`（`runs/` `stream_bridge/` `events/` `checkpointer/` `serialization.py` `context_keys.py`） | **本片补上**（`runtime/`），命名对齐 |
| runtime 的体量 | `runs/manager.py` 103KB + `worker.py` 104KB：RunManager 状态机、RunRecord、冲突检测、重试策略、Redis 桥、run 持久化 | **只取三样**：事件契约、运行上下文、入口/取消/终态。**不做 run 持久化**——那条线是 journal 的 |
| 谁依赖 runtime | 40+ 文件，含 `agents/`、`sandbox/`、`mcp/`、app | 同方向：`sandbox/` / `knowledge/` / `journal/` / `models/` → `runtime`；`agents/` → `runtime` |
| 运行上下文怎么传 | `ContextVar`（`user_context.py` / `trace_context.py` / `secret_context.py`） | **对象注入**（`RunContext` 经 langgraph `context=`）——**刻意分歧**，理由见 ADR-0012 §2（拒绝进程全局） |
| 存储 / checkpointer | runtime 持有 provider 单例（`get_checkpointer()` / `get_store()`） | 全在 `app.state`，runtime 不持有任何长命物（ADR-0007 / ADR-0011 照旧） |
| 门面 | `runtime/__init__.py` 只 re-export `runs` 与 `stream_bridge` 的公开面 | 同一手法，re-export 三个子包 |

一句话：**方向对齐、体量裁剪**。DeerFlow 的 runtime 是服务端的 run 管理器；EvaAI 的是
「契约 + 一次运行」，因为单用户本地部署不需要状态机、冲突与重试。

## 硬约束

- **`runtime` 不许 import `agents`，也不许 import 任何工具包。** 由
  `tests/test_harness_boundary.py` 的第二条测试守。搬 `run_agent` 时最容易破的就是它（
  `LEAD_LANE` 与 `final_text` 原先都在 `agents` 里，本片把两者分别参数化与搬成 `runs/state.py`）。
- **`RunContext.sandbox` 的标注必须是 `Any`，不能写成 `SandboxLayout`。** 本类型会随
  `ToolRuntime[RunContext]` 进工具的参数 schema，而 pydantic 建 schema 时会**真的解析**字段标注：
  写成具体类就得在运行期 import `evaharness.sandbox`，那正好成环。实测症状是一条
  ``PydanticUserError: `task` is not fully defined; you should define `SandboxLayout```
  ——**工具在第一次被调用时炸，不是启动时**，启动期 smoke build 抓不到。访问器
  `sandbox_layout()` 对外的返回类型仍是 `SandboxLayout`（函数标注不被 pydantic 解析）。
- **`event/translate.py` 里「认不出来的载荷返回 `None`」不能改成「兜底当成 state 快照」。**
  原实现是 `if messages / elif custom / else(values)` 的顺序敏感结构，custom 排在 else 之前是硬要求；
  现在调用方用 `produces_event()` 做正向判定，陷阱在结构上消失了，但**两种 `None` 的含义不同**
  （「不是事件」vs「是 values 快照」），别糊在一起。
- **老 journal 必须能回放。** `event/contract.py` 的遗留类只读不改。

## 后果

- **三条跨包依赖边消失**：`sandbox` / `knowledge` / `journal` / `models` 不再 import `agents`；
  `agents → sandbox` 的真实运行期 import 也没了（`SandboxLayout` 原先只是 `LeadContext` 的字段标注）。
- **取消协议的两半住到一起**，`chat.py:_make_cancellation` 只剩「把 registry 与 run_id 绑起来」这一句。
- **`agents/` 缩小**：`lead_agent/runner.py` / `custom_events.py`、`agents/messages.py`、
  `agents/events.py`、`agents/options.py`、`agents/middlewares/context.py` 六个文件搬走或删除。
  `agents/lead_agent/__init__.py` 从 12 个导出降到 7 个。
- **测试**：53 个测试文件里 20 个改了 import 路径；`test_run_registry.py` 从 app 测试变成 harness
  测试（仍在同一位置，只是被测对象换了层）。`evaharness` 覆盖率 94.6%（门槛 80%）。

## 不做 / 范围外

- **不引入 DeerFlow 的 RunManager / RunRecord / 状态机 / conflict / 重试 / Redis 桥 / run 持久化。**
  单用户本地部署不需要；真需要时那是新的一片。
- **不把 deerflow 的 `ContextVar` 那套搬过来**：本项目的立场是「运行期没有隐式全局」，照旧。
- **不新开 `evaharness/context` / `evaharness/events` 顶层包**：按决策 1 收进 `runtime/`。
- 不做 `evaharness/config` 的 harness/部署 schema 分层、不做 `app/gateway/app.py` lifespan 拆分、
  不做 journal 的会话元数据拆分、不动 `contracts/` 与前端。

## 遗留债

- **类型级仍有一个环**：`sandbox → runtime.context`（运行期真依赖）↔ `context/contract.py` 里
  `SandboxLayout` 的标注（只活在 `TYPE_CHECKING`）。**运行期无环**（守卫测试保证），但类型图上有。
  彻底的解法是在 `context/contract.py` 定义一个 `SandboxProvider` Protocol（只声明工具要用的
  `session(session_id)`），代价是要先确认 `SandboxLayout` / `Sandbox` 的哪些成员属于工具侧契约
  ——那是设计工作，不是搬移，所以本片不做。
  （参照项目同病：它的 `runtime → agents.goal_state` 也是一条反向小边。）
- **`runtime → checkpoint`**：`runs/run.py` 要 `lead_thread_id` 派生坐标。这条边是向下的、无环，
  但严格说让 runtime 认识了 checkpointer 的坐标约定。将来要拆时，缝在
  `checkpoint/__init__.py` 那三个 `*_thread_id` 函数上。
