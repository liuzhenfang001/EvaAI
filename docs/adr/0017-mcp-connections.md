# MCP：第三层配置第一次有了消费者，连接与图同寿命

T13 之前，agent 能做什么只由 `config.yaml` 的 `tools:` 决定——**扩大能力必须我们自己写 `@tool`**。
`extensions_config.json` 的 `mcpServers` 是个空占位，`load_extensions()` 在生产代码里**零调用**。

这不是没人想到，而是当初的**决定**。ADR-0012 白纸黑字写着：「`loader.load_extensions()` 至今没有被
任何地方调用，本片不碰它——现在把工具塞进去等于把一个半成品坐实成契约。」本片就是那个把它坐实的
切片。所以这份 ADR 的第一件事是回答「为什么现在可以碰了」，第二件是记录两条被实测推翻的预设。

## 决策

**1. 配置落在 `extensions_config.json` 的 `mcpServers`，并且这一层从此是必需文件。**

两层配置的分工是「主配置 = 我们自己的东西，extensions = 外来的扩展」。MCP server 是**外部进程 /
服务**的声明，正好是后者；而 `tools:` 那段有一句很强的话——「工具是 agent 的能力边界，所以没有隐藏
的默认集」——把动态发现来的外部工具混进去会把这句说糊。

`load_extensions()` 从前不解析 `$ENV`、不校验版本、也没有 typed schema，现在多了一份
`ExtensionsConfig`。**不做 `$ENV` 解析是延续，不是遗漏**：stdio 的 `env` 由 fastmcp 自己展开
`${VAR}`；`headers` 里要放密钥是另一条设计（OAuth 已经被排除在范围外）。

两个类都用 `extra="forbid"`，**与 `app_config.py` 里那些 `extra="allow"` 的类相反**。理由：那些类的
额外键有归宿（模型的其余键透传给构造函数、工具工厂会因未知键抛 `TypeError`），这里没有下游构造器
接住它们——允许 extra 就等于让 `comand: npx.cmd` 这种拼写被静默忽略，症状是「配了 server 却一个工具
都没有」。代价是抄别人的 `mcpServers` 时若有别的键会启动失败；这是有意选的失败形状。

**2. 库用 `langchain.mcp`，不是 `langchain-mcp-adapters`。**

参照项目用的那个包**已经被归档**（0.3.2 是最后一版，README 置顶「不再积极维护」），官方把 MCP 支持
迁进了 `langchain.mcp`（`langchain[mcp]`，beta）。我们装着的 langchain 1.4.2 **已经带着它的源码**，
只差 `uv add` 拉 fastmcp。两者**互斥**（旧包要 MCP<2，会把新模块要的 2.x 静默降级），所以这是个
不可逆的选择——而冻结的那个不值得我们关上新版的门。

代价照实记：它是 beta，import 会打 `LangChainBetaWarning`；用得深的话 API 变更会咬人。我们的用法面
很窄（建 adapter、`list_tools`、保住 context），所以接受。

**3. 每个 server 一个 `MCPAdapter`，逐 server try/except。**

`MCPAdapter` 连接失败**直接抛**，不做隔离；多 server 走 `MCPConfig` 时甚至「整队共享一个协议 era」。
要「一个 server 挂了不拖累别的」只有一条干净的路：每个 server 一个 adapter，各自进出、各自兜。
副作用是好的——单 target 的 adapter **不给工具加前缀**，所以命名权落在我们手里（见下条）。

**4. 工具名一律 `mcp__<server>__<tool>`。**

实测官方 filesystem server 提供 `read_file` / `write_file` / `edit_file` / `search_files`，而前两个
正是我们自己的业务工具名。撞名不是假想。

为什么**始终加**而不是「撞了才加」：后者会让工具名随「今天有没有新加一个同名业务工具」而变，而改名
是有后果的——模型在对话里学到的名字会失效，历史 RunJournal 里记的工具调用行也会对不上。双下划线
而不是单下划线：`{server}_{tool}` 在有嵌套下划线时分不清 server 名到哪结束。

**5. MCP 工具也给子 agent。**

`present_files` 当初被剔，是因为它靠 `runtime.stream_writer` 发自定义事件，而子 agent 用 `ainvoke`
跑、那个 writer 是 no-op，于是**静默失效**。MCP 工具不依赖那个通道，剔它的理由在这里不成立。

代价是 `extra_tools` 必须同时喂**两处**（子图与主图），漏一处又是一次静默失效。所以
`AgentFactory.lead()` 里只造一份 `combined`，两处都用它，并且有一条测试**专门盯子图那一半**。

**6. 连接的生命周期 = 一次运行。**

这是本片最难的一处，因为它与「工具必须在 `create_agent(tools=...)` 之前拿到手」互相夹——两个约束
一夹，落点只剩一个：**图必须在那个 run 生成器里建**。

于是 `chat.py` 的两条路由各包了一层「一次运行」的异步生成器：
`进 MCP 连接 → 建图 → 跑 run_lead_agent`。这落在 ADR-0011 已经定好的契约上——「**图的寿命 = 响应流
的寿命**」——连接跟着图走，取消时走同一条清理路，不新增任何生命周期概念。

**代价是 ADR-0011 记的那条后果要改**：建图错误从「返回 `StreamingResponse` 之前炸 → JSON 500」变成
「流里抛 → SSE error 帧」。这不是退步：那条 ADR 自己写着 500「比 error 帧更难察觉」（前端期望 SSE，
收到 JSON 表现为「没有任何反应」），而且配置写错、`use` 指向不存在的类、模型构造失败这几类已经被
启动期 smoke build 挡在服务起来之前了。**请求参数的校验（400）与 `ensure_session_dirs` 仍留在端点
里**——那些该有 HTTP 状态码。

**7. 启动期探测一次，失败只警告。**

它验的是「外部依赖**现在**能不能用」，与 smoke build 验的「我们自己的装配连线通不通」是两件事。
界线写死：**装配错 → 拒绝启动；MCP 不可用 → 响亮警告后照常起。** 把第三方 server 的可用性提到
「EvaAI 打不开」的高度，等于让一次网络抖动把助手变成砖头。

运行期的降级只记日志，不在对话里提示——确定性的错误在启动期已经喊过了，剩下多半是瞬时抖动，
不值得打断对话。

**8. 撞名的 MCP 工具丢掉并记日志，不抛。**

与启动期那套「拒绝启动」不同。`mcp__<server>__<tool>` 是确定性前缀，真与业务工具或 `task` 撞上只
可能是我们自己的 bug；按 MCP 一贯的取向（一个 server 的问题不该连累这一次运行），丢掉并点名即可。

## 实测（2026-10-08，本机 Windows 11 + fastmcp 4.0.11）

这些数字是下面几条硬编码的依据，不是转述文档：

| 验的什么 | 结果 |
|---|---|
| 长命连接做不做得到 | **能**。官方文档写明「保持 adapter 的 context 打开就会复用同一连接」；底层 FastMCP 客户端是引用计数可重入的 |
| 不在外层进 context 会怎样 | `list_tools()` 内部自己 `async with self`，返回的工具**每次调用**再各自 `async with client`——stdio 下就是每个工具调用重开一个子进程 |
| `keep_alive` 的默认值 | **True**，而它会漏进程：连开三轮后 `node.exe` + `cmd.exe` 各多两个并且**再也不还**（12 → 14 稳定）；显式 `False` 三轮之后仍是 12 |
| 超时给在哪 | `Client(timeout=)` → 会话的 `read_timeout_seconds`；`init_timeout` 不传是 `None`（**握手没有超时**）。`MCPAdapter` 会 `client.new()` 克隆一份客户端，那是浅拷贝、会复制 `_session_kwargs`，所以两个超时克隆后仍有效 |
| Windows 上 `command: npx` | **起不来**：`shutil.which("npx")` 返回 `D:\develop\npx`——一个无扩展名的 sh 脚本，`CreateProcess` 报 `WinError 193`，错误信息里没有一个字指向真正的原因。`npx.cmd` 可以 |
| 首次 npx 的代价 | 30.9 秒（含下包），之后更快 |

## 三处 `aclosing`（本条修的是一个既有缺陷）

`async for` 提前退出**不会**关上游生成器——这是 Python 语义，不是 bug。而 T13 之前没有 run 级的异步
资源，所以没人发现；MCP 连接一进来，它立刻变成真泄漏：客户端断开时，包在生成器里的
`AsyncExitStack.__aexit__` **不会被 await**，npx 子进程一直留着。

修法是给三层各套一层 `contextlib.aclosing`：`sse_consumer`、`journaled`、`_run_lead`。这样
`CancelledError` 穿出时形成 `sse → journaled → _run_lead → AsyncExitStack` 的 `aclose` 链，不需要改
框架、不需要 shield。

**已知边界，写下来免得被误读成「已经全兜住」**：如果最外层的消费者**既不 `aclose` 也不抛**、只是
弃置生成器（`async for ... : break`），那一层链不会当场跑——弃置的异步生成器要等 GC 的 finalizer，
而那是不确定的。所以每一层都必须自己套 `aclosing`，不能指望下游负责。
`test_gateway_mcp_run.py` 里有一条测试专门把这个边界写明。

## 后果

- **第三层配置有了契约**：`extensions_config.json` 从此是必需文件（缺失即拒绝启动，与 `.env` /
  `config.yaml` 同口径），并改成了「模板入库、真文件忽略」——另两层一直是这个口径，它之前只是因为
  内容是空占位才无所谓。README 的快速开始因此多一步 `cp`。
- **`load_extensions()` 有了第一个消费者**，ADR-0012 那句「不碰它」到此解除。
- **建图挪进了 run 生成器**（见决策 #6），ADR-0011 的后果一节被这份 ADR 更新。
- **`AgentFactory.lead()` / `plan_exec()` 签名只多了 keyword-only 的 `extra_tools`**，**没有**变成
  async——异步的部分（连 MCP、拉工具）发生在生成器里、建图之前。这一点值得记下来：一度以为建图挪进
  异步上下文就得让工厂方法变 async，其实不必。
- **`registry._check_names` 提成公开的 `check_tool_names`**。「提成公开」是为了让运行期那条路能复用
  同一套措辞，但运行期**并不调它**（见决策 #8），所以它实际仍是启动期校验。
- 前端**零改动**：运行期降级只记日志，用户可见面没有变化。

## 不做 / 范围外

- **工具的延迟披露**（工具多了先搜工具再调）：不先接一个几十工具的 server 看看 token 到底涨多少，
  就提前加一层「用工具找工具」的间接，是把复杂度花在未经验证的地方。
- **OAuth**、**per-server 工具白/黑名单**、**健康检查与自动重连**、**resources 与 prompts**、
  **EvaAI 自己作为 MCP server 被接入**。
- **`sse` / `websocket` transport**：前者的协议层已弃用，后者要另维护一条连接路径。
- **跨请求复用连接**：本片是每请求建连。参照项目按 `(server, 用户:会话)` 池化，那是为多租户准备的；
  单用户用不上，而且它把「发现工具」这一步挪进了请求路径。
