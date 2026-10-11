# 沙箱与文件工具上移到 harness：机制进框架，策略经运行上下文注入（T17）

ADR-0012 把虚拟路径沙箱与文件工具放在 **app 层**，理由原文是「有哪些工具、叫什么名字、开放哪些
参数，都是业务决定」。但落地下来的形态与那句话对不上：**沙箱机制**（虚拟路径归一化、两层包含性
校验、挂载表渲染）与**六个文件工具**一点都不业务——它们是任何 agent 框架都要有的东西；而那条
理由真正针对的是**工具注册表**（配置驱动、名字由部署决定），不是工具的实现。

于是分界变得别扭：harness 里早就有 `session_id()` / `ToolRuntime` 这套「工具该怎么工作」的契约
（`evaharness.agents.middlewares.context`），却**一个工具都没有**；而沙箱的宿主根只能靠一个
**模块全局** `app/tools/sandbox.py:SANDBOX_ROOT` 传播，9 个测试文件靠 monkeypatch 那个字符串把它
重定向到 `tmp_path`。

参照项目 DeerFlow 的沙箱与 `@tool("bash")/ls/glob` **全在 harness 里**
（`deerflow/sandbox/tools.py`，2409 行）——但它为此用了**进程级 provider 单例**
（`get_sandbox_provider()` / `set_sandbox_provider()`），那正是 ADR-0012 §2 明文拒过的形态
（「不读全局，工具私有参数走工厂闭包」）。

本 ADR 记录机制上移，以及**不用全局**的那条替代路径。

## 决策

**1. `evaharness.sandbox` 拿到四样：路径机制、容器机制、六个文件工具、bash 工具。**

```
evaharness/sandbox/
  paths.py     Mount / DEFAULT_MOUNTS / DEFAULT_MOUNT / PathNotAllowedError
               SandboxLayout（root + mounts + default_mount）
               Sandbox（归一化、两层校验、display/mask）
  docker.py    Completed / DockerError / Runner / DockerCli / SandboxSpec
               容器生命周期（懒创建、复用、reconcile、按会话删）
  file_ops.py  六个文件工具（ls / read_file / write_file / str_replace / glob / grep）
  bash.py      make_bash 工厂 + render
```

**harness 的依赖零增长**：这批代码只用 stdlib + langchain（已在依赖里）。dependencies 一个字
没动——这是「机制而不是业务集成」的一个硬证据。

**2. 宿主根与挂载表是**策略**，由部署构造一次 `SandboxLayout`，经**运行上下文**注入。**

`LeadContext` 加第四个字段 `sandbox: SandboxLayout | None = None`，并新增访问器
`sandbox_layout(runtime)`（与 `session_id` 并排，同样认「对象」与「dict」两种形状）。

传导链只有一条（T16 删掉 Plan-Exec 之后更短）：

```
app lifespan: layout = build_layout()  →  app.state.sandbox_layout
   ├─ chat.py: run_lead_agent(..., sandbox=layout) → runner → LeadContext(..., sandbox=layout)
   │     └─ task 工具：子 context 原样继承（子 agent 与主 Agent 看**同一份文件**）
   ├─ chat.py / files.py / threads.py：直接从 app.state 取
   └─ 工具侧：sandbox_layout(runtime).session(session_id(runtime))
```

**为什么是 context 而不是 `RunnableConfig.configurable`**（那条更省事）：中间件用的
`Runtime` **没有 `.config`**（见 `langgraph/runtime.py`），而 `context` 是这一层唯一同时可达
工具与中间件的通道；`session_id` 已经在那儿，布局与它同类（「这次运行要用的东西」）。
另有一条实证：`tests/test_lead_agent_subagent.py` 对 config 做了严格相等断言，走 config 会打红它。

**为什么字段有默认值 `None`**：不碰文件的图（纯推理的测试、中间件单测）不必构造它——8 处既有的
`LeadContext(...)` 构造点因此零改动。而**用了文件工具却没注入布局**的运行会**响亮地失败**
（访问器取不到就 raise，与 `session_id` 同口径），不是悄悄写进某个猜出来的目录。

**3. 挂载表由 harness 给默认，app 可覆盖。**

`DEFAULT_MOUNTS`（`/workspace`、`/output`、`/input`、`/skills`）落在 harness，`app` 只需要给一个
根。这是对 ADR-0012「挂载表是业务决定」的**修正**：四条名字是**惯例**（与参照项目同构），真正
随部署变的是**根在哪儿**——而那正是 `SandboxLayout` 里唯一没有默认值的字段。

要加挂载点或改只读策略，就在构造 layout 时传 `mounts=`——**工具侧零改动**，它们从上下文拿到的
就是这个对象。挂载表驱动的每一处都跟着走：错误文案里的「只支持这些虚拟根」、相对路径的落点、
按会话建目录的集合、`/skills` 的宿主位置，全部从 layout 现算（T17 之前它们是模块全局派生的，
自定义表会让文案与实际规则不一致）。

**4. `present_files` 留在 app。**

它依赖沙箱只是历史。它留在 app 的**新**理由是**交付语义**——产出一条 `files_presented` 事件、
只放行 `/output`。ADR-0015 §3 当年写的「它依赖 `app.tools.sandbox.Sandbox`，而 boundary 测试
禁止 harness import app」这条理由**到期了**，已在那边标注。

**5. 两个必须拆开的地方。**

- **哨兵文件的创建从 `check_docker_ready` 拆出去**。它在**启动期**由注册表建工具时调用，那时
  还没有 layout（工厂只吃 `config.yaml` 的额外键）。现在 `check_docker_ready(image)` 只做
  CLI / daemon / 镜像 / 能力四项；`ensure_mount_check(layout)` 由 **app lifespan** 在启用 bash
  时调一次。
- **`docker.py` 里 6 处运行期读全局的地方全部改成吃 layout**：`deployment_id`、
  `mount_check_dir`、`ensure_mount_check`、`run_command`、`reconcile_orphans`、
  `remove_session_container`（`render` 也跟着要 layout，因为掩码要用它）。

**6. 掩码收敛成一份。**

原先 `Sandbox.mask`（窄）与 `docker_sandbox.mask`（宽，含 `\\?\`、正斜杠、小写盘符）是两份。
现在住在 `SandboxLayout.mask`，`Sandbox.mask` 委托给它。启动期那几条 docker 报错的详情走一条
**不掩码**的 `_plain_detail`，理由写在函数 docstring 里：那时没有布局，而 CLI / daemon / 镜像
这几条报错不涉及挂载、不带宿主路径，且文案是给运维看的中文。

## 与参照项目的对照

| 处 | DeerFlow | EvaAI |
|---|---|---|
| 沙箱与文件工具放哪 | harness（`sandbox/tools.py`，2409 行） | harness（`evaharness/sandbox/`）——**这一条对齐了它** |
| 部署策略怎么进工具 | 进程级 provider 单例（`get_sandbox_provider()`） | **构造一次 `SandboxLayout` 经运行上下文注入**——**刻意分歧**，理由见 ADR-0012 §2 |
| 挂载表 | 配置项 + 一批 `_get_*_container_path()` 帮手 | `DEFAULT_MOUNTS` + 可覆盖的 `mounts` |
| 容器内怎么跑命令 | `-p 8080` 暴露 AIO 服务，走 HTTP API | `--entrypoint sleep` 关掉那套服务，`docker exec`（ADR-0019） |
| 资源与并发上限 | 无 `--memory/--cpus/--pids-limit`；有 `replicas` + LRU | 有资源上限；**无并发上限**（单用户，记为欠账） |

## 后果

- **`app/tools/` 只剩四个文件**：`builtin` / `present_files` / `registry` / `web`（外加本片新增的
  `sandbox_layout.py`）。业务层不再有任何沙箱实现。
- **运行期没有模块全局决定沙箱根**。`SANDBOX_ROOT` 只剩下 `app/tools/sandbox_layout.py` 里那一个
  ——它是**部署约定**（`backend/.evaai`），明说属于 app。
- **测试不再 monkeypatch 全局**：9 处 `monkeypatch.setattr("app.tools.sandbox.SANDBOX_ROOT", …)`
  换成「造一个 `SandboxLayout(root=tmp_path)` 塞进上下文」，收口在 `tests/tool_fakes.py` 的
  `lead_context()`。
- **harness 的覆盖率门槛从此覆盖这 ~580 行**。`DockerCli.invoke` 与 `_read_bounded` 原先只有
  skipif 的集成测试盖着，本片补了假 `Popen` / `BytesIO` 的单测（**不需要 docker**）——
  `docker.py` 覆盖率 97%。
- 文件工具与 bash 的 `use:` 路径变成 `evaharness.sandbox.file_ops:*` / `evaharness.sandbox.bash:make_bash`。

## 硬约束

- **工具实例跨请求共享**（ADR-0011），布局因此必须无状态、可跨请求复用——`SandboxLayout` 是冻结
  dataclass，构造时 `resolve()` 一次就再也不变。
- **`context.py` 对 `SandboxLayout` 的导入必须留在 `TYPE_CHECKING` 里**：`evaharness.sandbox` 的
  `__init__` 会拉进 bash / file_ops，而它们反过来要 import `context` 取访问器——运行期导入成环
  （实测撞过一次）。访问器只需要把 context 里那个值原样取出来，不碰这个类。

## 不做 / 范围外

- **不引入进程级 provider 单例**——本片的目标正是「不用全局」。
- **不动 `present_files` 的交付语义**，也不动提示词里教模型的四个路径。
- **不把工具注册表搬进 harness**（ADR-0012 那条决定照旧：装哪些工具、叫什么名字、开放哪些参数，
  仍由 `config.yaml` 说了算）。
- **不顺手加 DeerFlow 那三样**：容器**并发上限 + LRU 淘汰**（它叫 `replicas`；我现在每会话一个、
  无上限）、**输出上限与超时做成配置节**（它叫 `bash_output_max_chars` 等；我是模块常量 + 工厂
  参数）、**`allow_host_bash` 那种显式闸门**。这三样是**加功能**，而这一片是**纯搬移**——混在
  一起会让「行为一模一样」这条验收标准失去意义。各自成片。
- **资源上限反而要留着**（`--memory` / `--cpus` / `--pids-limit`）：那是参照项目的空缺，
  不是它的设计。

## 遗留债

- **容器没有并发上限**：每会话一个、随会话数增长。单用户场景够用，但这是参照项目有而我们没有的
  一条（它有 `replicas` + LRU）。
- **`/skills` 的宿主目录**由挂载表决定，而不是一个独立配置项——自定义挂载表若不含 `/skills`，
  `skills_view_root()` 会**如实报错**（与 ADR-0012「如实报不存在比造个空目录好」同一取向）。
