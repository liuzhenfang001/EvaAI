# Plan-Exec 下线：事件契约搬家，产线删除（T16）

Plan-Exec 从 **T8 起就不是默认路径**（ADR-0010）：主 Agent（ReAct）接管对话，它退到
`/chat/plan/stream` **封存**——路由与测试都保留，但没有 UI 入口。当时的理由是「将来以主 Agent
的一个子能力回来」。

代价在之后每一次动 agent 层时兑现：每请求建图、工具表、中间件、上下文形状……每一次都要在
planner / executor / replanner / orchestrator / fanout 那套里再走一遍。最近一次是沙箱上移
（T17 的规划）：执行单元那个 **dict 形状的 context** 平白多出一处传导点，而且它没有
`context_schema`，与主路径是两套形状。

本 ADR 记录把它**删掉**，以及删之前必须先拆开的两个耦合。

## 决策

**1. 产线全部删除。**

- `evaharness/agents/plan_exec/`（planner / executor / replanner / orchestrator / fanout / schemas）
- `agents/react.py` 的 `make_react_agent`——**它唯一的生产使用者就是执行单元**。主 Agent 走
  `agent.py` 里自己的封装（当年明确记为「共用一个封装只会让两边互相牵制」），所以它随产线一起走。
- `POST /chat/plan/stream` 路由、`AgentFactory.plan_exec` 与 `PlanRoles`、启动期 smoke build 的
  `smoke.plan_exec()`
- `app/prompts.py` 的 `PLANNER_PROMPT` / `REPLANNER_PROMPT` / `EXECUTOR_PROMPT`
- 7 个 `test_plan_exec_*.py` 与 `test_gateway_chat.py`

**2. 事件契约从 `plan_exec/events.py` 搬到 `agents/events.py`。**

那是 ADR-0010 记下的一笔债（模块自己的 docstring 就写着「搬到 ``agents/events.py`` 才是正确的
位置……欠账记在 ADR-0010」）——`RunEvent` 是 journal / SSE 的**对外契约**，却住在封存路径的包里，
`journal/{compaction,journal,stores}.py` 反过来得 import 它。

**3. 所有事件类都保留，包括 `PlanEvent` / `ReplanEvent` / `TaskStartEvent`。**

这是本片最容易搞错的一条：`RunEvent` 是**判别联合**（`Annotated[Union[...], Field(discriminator="type")]`），
而 `stores.py` 直接 `JournalEntry.model_validate(json.loads(line))`。**删掉任何一个成员类，
含那种事件的老 journal 一读就抛**——而「journal 是要能回放的老数据」是本项目的硬约束。

所以那几个类变成**只读的遗留契约**：只被解析、不再被生产。它们的载荷类型
（`PlanTask` / `TaskResult` / `TerminalTaskStatus`）跟着内联进事件模块，并在注释里点名这件事。
**改它们之前先想清楚「老数据还能不能回放」。**

**4. `mode` 字段继续写，值恒为 `react`。**

前端 `runState.ts` 的缺省是 `"plan"`，而且那是**历史回退**语义（T8 之前写下的 journal 行里没有
这个字段）。后端要是不再写 `mode`，前端会把**每一轮新对话**都标成 plan。这个字段现在是纯粹的
**回放兼容**，不是路径分叉。

**5. 前端一行不动。**

任务面板、`runState.ts` 的 `plan` / `replan` 分支、`runState.test.ts` 全部保留——它们从此只在
**回放老会话**时被用到。删掉它们等于让 T8 之前的会话在界面上缺一块，而那批数据是真实存在的。

**6. `tests/plan_exec_fakes.py` 拆成 `tests/fakes.py`。**

它是个**被误名的通用模块**：16 个测试文件 import 它，其中只有 4 个用到 planner/executor 那部分。
通用部分（假模型、`fake_app_config`、`collect`）搬进新名字，plan 专用的三个 fake 与两个
`StructuredFake`（删完产线后无人使用）一起删掉。

**7. 借产线造事件的测试要改事件源。**

`test_gateway_threads.py` 的 `_run()` 与 `test_gateway_chat.py` 原先用假 planner + 假 executor
**产出事件**来测 SSE 与 journal 回放——它们测的是网关，不是 Plan-Exec。

- `test_gateway_threads.py`：事件源改成**主 Agent + 假模型**（`GenericFakeChatModel` 按空白切块
  流式输出，所以带空格的一句仍会产出多条 `messages` 增量，归并与回放那几条断言的前提不变）。
  `task_id` 从 `t1` 变成 `LEAD_LANE`（`main`）。
- `test_gateway_chat.py`：**整份删除**——它的题目（plan 帧 / summary / task 事件的接线）随那条
  路径一起消失。通用 SSE 映射在 `test_sse.py`，主路径的 `run_start` 契约在 `test_lead_agent.py`。
- `test_react.py` → `test_middlewares.py`：只留 `test_build_default_middlewares_order`。那条断言
  与 Plan-Exec 无关（默认链是主图与子图共用的），而且它是**唯一**钉住默认链自身顺序的地方
  （`test_lead_agent` 那几条是拿默认链跟自己比，抓不到默认链被调序）。

**8. 坐标形状的 context 兼容分支留着。**

`middlewares/context.py` 的两个访问器同时认「带 `session_id` 的对象」与「只有 `thread_id` 的
dict」。dict 那一支的**生产者**（执行单元的 `_astream`）随产线没了，但形状还在契约里：中间件的
单测一直用裸 dict 驱动，而且 T17 要加的沙箱访问器也会照同样的写法认两种形状。测试里的
`PLAN_EXEC_CONTEXT` 因此改名 `COORDINATE_CONTEXT`——**名字指向的东西没了，形状还在**。

## 后果

- harness 少了约 1000 行产线代码，测试少了约 1900 行。覆盖率 95.21% → 93.53%（阈值 80% 不变）。
- 上下文只剩**一种**形状（`LeadContext`），agent 层的传导点少一处。
- `evaharness.agents` 这一层的公开面只剩 `events` 这个契约，加 `lead_agent`。
- `config.yaml` / `config.example.yaml` **不受影响**（Plan-Exec 从来没有配置项）。

## 取代清单（照着读旧 ADR 时注意）

| 旧 ADR | 哪些部分作废 |
|---|---|
| [ADR-0004](0004-nested-plan-exec-and-react.md) | 整条：顶层不再是 Plan-Exec 嵌套 ReAct |
| [ADR-0006](0006-orchestration-as-async-generator.md) | 整条：`run_plan_exec` 这个编排载体没了。**思路仍有效**——`run_lead_agent` 是同一形态的 async 生成器 |
| [ADR-0008](0008-run-journal-and-parallel-dispatch.md) | 「按批并行派发」那一半（plan 任务的 fanout）；**RunJournal 那一半照旧**，且更重要了 |
| [ADR-0010](0010-lead-agent-as-default-path.md) | 「封存、路由保留」的处置：T16 改为删除。**「主 Agent 是默认路径」这条决策不变** |
| [ADR-0013](0013-cancellation-resume-and-step-budget.md) | 已经自己对齐过（T5 随 T8/T9 重新对齐），无需再改 |
| [ADR-0015 §3](0015-present-files-and-download.md) | 「`present_files` 只能放 app——它依赖 `app.tools.sandbox.Sandbox`，而 boundary 测试禁 harness import app」这句的理由：T17 会把沙箱搬进 harness，`present_files` 留在 app 的理由此后是**交付语义**，不是依赖方向 |

## 不做

- **不删事件类**（§3）。
- **不动前端**（§5）。
- **不动 `mode` 这个契约字段**（§4）。
- **不清理老 journal 数据**——它们是回放的基础。
