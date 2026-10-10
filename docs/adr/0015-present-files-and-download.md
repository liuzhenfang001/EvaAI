# 文件交付：`present_files` 工具、custom 流与下载路由

在此之前后端**完全没有**文件下发通道（没有静态挂载、没有下载接口），事件契约里也没有任何
文件引用字段——模型产出了文件，只能靠 `SYSTEM_PROMPT` 求它「用一句人话告诉用户文件放在哪」。
用户真正想要的是**可下载的卡片**（参照项目 DeerFlow 就是这样）。

本片把这条链打通：模型通过显式工具声明「这些是交付物」→ 一条 `files_presented` 事件 → SSE 到
前端画卡片 / RunJournal 落盘供回放 → 卡片上的链接打到新的下载路由取字节。

## 决策

**1. 用显式工具，不是后端自动扫 `/output`。**

扫目录的话「什么该给用户看」由目录内容决定，模型没有任何控制权（没法只挑一部分，也没法说
「这个是草稿先别给」）；而「呈现给用户」本来就是一个**意图**，该由模型表达。代价是要改提示词
（不告诉模型调它，这个工具永远不会被调用，整条链路是死的），而且模型可能忘了调——忘了的
后果是**没有卡片**，不是错误。

**2. 事件经 `runtime.stream_writer` + `stream_mode="custom"` 出海，不走 state 通道。**

DeerFlow 是把文件列表写进 LangGraph 的 state channel（`Command(update={"artifacts": ...})`），
前端用 `useStream` 读 `thread.values.artifacts`。**那条路我们不能照搬**：EvaAI 的前端不接
LangGraph state——它消费后端自建的 SSE 事件流，历史回放靠 RunJournal。照搬过来的 state 通道
在这儿**没有消费方**。

形似 DeerFlow 的「新增 state 字段、从 `values` 快照里 diff 出变化」也**否决**了，两个语义缺陷：

- `artifacts` 经 checkpointer **跨轮累积**，而 `values` 的第一帧就带着历史值——runner 拿不到
  「本轮开始前是什么」这个 baseline，于是要么把历史全部重放成卡片、要么得在开跑前额外读一次
  checkpoint（把前端的契约绑死到 checkpoint 上，而架构明确「回放靠 RunJournal」）。
- 写入要靠一个**去重** reducer，模型第二次展示同一个文件时 state 不变 → 没有新事件 →
  用户看不到第二次展示。

custom 通道则没有这些问题：一次工具调用一条事件，顺序与调用一致，落进 journal 就是一条独立
记录、逐字回放。

这不是新增输出通道——`RunEvent` 仍是唯一契约。工具 emit 的只是**载荷**（`custom_events.py`），
由 runner 校验并补上泳道后构造事件，所以 ADR-0006「一个产出两个消费」不变。runner 侧的映射是
**白名单**：不认识的 kind 一律忽略——自定义流是开放的，照单全收的话一个工具就能伪造 `run_end`
把本轮运行提前结束掉。

上线前用最小探针验过这条链路（工具体内 emit、断言事件出现在 `run_start` 与 `run_end` 之间），
探针已固化成 `test_lead_agent.py` 的永久用例。**同步与异步工具都能用**：langgraph 没有把
writer 的上下文丢掉。

**3. `present_files` 工具放 app 层——这是相对 DeerFlow 的一处被迫偏离。**

> **2026-10-10 修正**：下面这条理由**到期了**——沙箱与 `file_ops` 正被搬进 harness（T17），依赖方向将不再是理由。`present_files` **仍然留在 app**，但换一个更实在的理由：它是**交付语义**（产出一条 `files_presented` 事件、只放行 `/output`），而 `file_ops` 是通用的文件读写。

DeerFlow 把它放在 harness，因为它自己的 paths 模块也在 harness。我们的 `/output` 路径策略在
`app.tools.sandbox`，而 `test_harness_boundary.py` 禁止 harness import app。规则一句话：
**harness 拥有机制与契约，app 拥有策略与路径**（`task` 工具在 harness 是因为「派发」是机制且
不碰业务路径；`file_ops` 在 app 是因为要 `Sandbox`）。

工具只放行 `/output`，判据用 `Sandbox.mount_of()` 拿到的**权威挂载点**而不是
`path.startswith("/output")`（后者会被 `/output-x/` 骗过去）；后面再走 `Sandbox.resolve()` 的
第二层防护（符号链接与 Windows junction 逃逸在词法层看不出来）。失败一律 `raise`、不返回
`Error: ...` 字符串——又一处与 DeerFlow 的偏离，理由 ADR-0012 已经写死。

**4. 子 agent 的工具集里必须没有它。**

子 agent 用 `ainvoke` 跑、没有 custom 流模式，它那张图的 `runtime.stream_writer` 是个 no-op——
工具在里面 emit 的东西**石沉大海：不抛异常、不报错，只是卡片永远不出现**。这种静默失效比报错
难查一个量级，所以在 `AgentFactory.lead()` 里**结构性地**剔掉，而不是在工具里加运行期判断
（那种判断本身也可能写错，而且会把「为什么不行」散到两个地方）。语义上也对：交付物由主 Agent
统一交给用户，子 agent 只把「文件写到哪了」回报给它。

**5. 下载路由：`GET /threads/{会话}/files/{路径}`。**

与 `threads.py` 同前缀但独立成文件——那个是 journal 回放的读接口，这里是字节流。相同前缀让
URL 保持**会话作用域**（「谁的文件」在路径里一眼可见）。

- **只放行 `/output`**：`/output` 前缀由路由自己拼，`/workspace` `/input` `/skills` 天然不可达。
  要放宽得改路由，而不是指望调用方守规矩。
- **可执行内容强制附件**：`.html` `.htm` `.xhtml` `.svg` `.xml` 一律 `Content-Disposition:
  attachment`——模型写出来的 `.html` 若能在浏览器里 inline 打开，就等于让它在 API 的源上跑
  脚本。判据用**后缀**而不是 `mimetypes` 的结果：后者在 Windows 上会去查注册表，同一份代码在
  不同机器上可能给出不同答案，安全判断不能依赖一个会随环境变的输入。
- `?download=true` 强制附件：卡片上的按钮写的就是「下载」，而对 `.png` 这类浏览器自己会渲染的
  类型，光靠 inline 点开只会新开一个标签页。
- **不存在的会话要在磁盘上不留痕**：`Sandbox.__init__` 会顺手把三个目录建出来，所以路由先用
  新增的 `Sandbox.exists()`（纯存在性检查、无副作用）挡一道再构造它。宿主布局的知识继续只留在
  `sandbox.py` 一个地方。

**6. 预览复用同一个端点，面板占第三列。**

点卡片 → 右侧出现预览面板（代码/文本/Markdown/图片），**取内容用的是与下载完全相同的
URL**，只是不带 `?download=true`。这与参照项目 DeerFlow 是同一个设计：一个端点，靠那个
参数切换 `Content-Disposition`——**所以后端一行都不用改**。

三处刻意不照搬 DeerFlow：

- **面板状态不持久化**。DeerFlow 把「面板开没开、在看哪个文件」存进 sessionStorage，刷新后
  还开着。我们不存——本项目已经有一条明文规矩（写在 `ProcessBlock` 上）：UI 偏好放组件本地
  state，**不进 reducer**，因为 reducer 是「事件 → 状态」的纯投影、实时与回放共用，把「我
  点开过哪一轮」塞进去会污染回放。「我在看哪个文件」是同一类东西。
- **代码视图用现成的高亮，不引 CodeMirror**。DeerFlow 在预览里挂了一个 CodeMirror（可编辑）。
  我们只读，把文本包成一个**围栏代码块**再交给 [ADR-0014](0014-markdown-rendering.md) 那个
  Markdown 组件就够了——高亮、横向滚动、暗色样式全部免费。围栏长度按内容里最长的一段反引号
  算出来（`fenceFor`），因为文件本身就可能含 ```（Markdown 文件、文档里的示例）。
- **不预览 HTML**。DeerFlow 为它走了一条很重的路：Blob URL + `sandbox="allow-scripts
  allow-forms"` 且刻意不给 `allow-same-origin` + 注入 `<base href>` + 滚动位置 postMessage
  恢复。我们的下载路由本来就把 `.html` 归进「强制附件」的 active content，为预览单开一条更
  危险的通道不值得——它落到「不支持预览 + 下载」那一档。`.svg` 同理（见下）。

**面板是「真列」，不是浮层**——打开时对话**收窄**而不是被盖住，这样能一边看代码一边对照
回答。这与既有的任务编排面板（浮层、不占网格列）**有意不同**：那个是「顺带看一眼」的辅助
信息，盖住一点无所谓；预览是要对着读的内容。

**可预览的范围**由 `core/preview.ts` 的纯函数决定，判据**以后缀为主、MIME 为辅**：后端的
`mimetypes.guess_type` 认不出大量代码后缀（实测 `.go` 拿到 `null`），只看 MIME 会把代码文件
全判成「不支持」。`.svg` 刻意不在图片白名单里——它能带脚本（虽然 `<img>` 里嵌 SVG 是安全的，
但为它单开一条通道不划算），而且与下载路由把它归进 active content 的口径保持一致。

## 后果

- **事件契约扩了一个变体**：`FilesPresentedEvent`（`category="message"`，卡片属于「这一轮产出了
  什么」，要进对话投影）。`PresentedFile` 里只有虚拟路径，**宿主路径永不进事件**——它会一路漏到
  SSE、journal 与浏览器。
- **前端多一条投影分支**：`applyFrame` 的 `files_presented` 把文件挂到 `runId:task_id` 那个气泡
  上（挂运行级状态的话，下一轮 `run_start` 会把它整体重置掉）。`toFrame` 对未知事件本就原样
  透传，所以**回放不需要额外改动**——这条由测试钉住。
- **卡片只可能来自显式交付**：写进 `/workspace` 的文件不出卡片，这是有意的——工作台是干活的
  地方，`/output` 才是交付。
- **预览的截断判据建在 `FileResponse` 的 Range 支持上**（只取前 256 KiB）。那是 Starlette 的
  保证而不是我们写的，所以有契约测试钉着它——哪天响应类型换了、或上游改了行为，会先在那里
  红，而不是前端悄悄显示半截文件却当成全文。
- **判据是 `Content-Range` 里的总数，不是状态码**。这条踩过：`FileResponse` 对**任何**合法的
  Range 请求都回 206——100 字节的文件配 `bytes=0-262143` 照样是 206 加全文。拿状态码当
  「截断了」，结果是每个小文件都顶着一句「文件较大，只显示了前 256 KB」。真正的信号是
  `bytes 0-99/100` 里斜杠后面那个总数。这个判据抽成了纯函数 `isTruncated()`——埋在组件里
  的时候它没被测到，抽出来之后有 5 条用例钉着。

## 已知行为

- **下载到的字节与模型写的差一个 CR**：`write_file` 走文本模式，Windows 上 `\n` 落盘成 `\r\n`。
  这是文件工具**既有**的行为，本片只是第一次把原始字节交给用户，从而让它可见。要改成「原样字节」
  是 `file_ops` 的事，不在本片范围（测试里按内容断言，不逐字节比换行风格）。
- 若将来给工具加重试机制，同一调用执行两次会 emit 两次（正常路径没有重试）。测试里钉住
  「一次调用一条事件」。
- 封存的 Plan-Exec 路径（`/chat/plan/stream`）也会拿到这个工具，但它的 runner 没有 custom
  模式，调用静默无效。Plan-Exec 已封存，接受。

## 不做 / 范围外

- **自动扫 `/output` 出卡片**：见决策 1（将来若要兜底，是加一层 fallback，不是换掉显式声明）。
- **`/workspace` 的下载**：`/output` 是「交付成品」的挂载点，放宽是另一个决定。
- **PDF 预览**（以及音频/视频）：DeerFlow 用 iframe 直接指向端点做这几类。本片只做文本/代码/
  Markdown/图片——那几类在本地助手场景里不常见，而每一类都要多一条安全与降级路径。
- **预览里编辑文件**：DeerFlow 有一套带乐观锁的 `PUT` 编辑接口。本片只读。
- **删除交付物**：`DELETE /threads/{id}` 目前清 journal 与 checkpoint，不删 `backend/.evaai/`
  下的文件（ADR-0012 已记为遗留债）。
