# `log_level` 第一次有了消费方：只配自己的命名空间

`config.yaml` 从 M1 起就有 `log_level: info`，`AppConfig` 从 M1 起就有这个字段——但它**从来没有
消费方**。全仓只有三处提到它：字段声明、两条配置解析测试。这是一处**死配置**。

本片把它接上。触发点是 T18：知识库验收里那条「不改 → 跳过重建（**有日志/事件可观察**）」在真实
部署下**其实不成立**——因为那些 `logger.info(...)` 一条都不会出现在控制台上。

## 实测（2026-10-12，本机，uvicorn 默认日志配置）

| 验的什么 | 结果 |
|---|---|
| uvicorn 的默认配置碰 root 吗 | **不碰**。`LOGGING_CONFIG` 只配 `uvicorn` / `uvicorn.error` / `uvicorn.access` 三个 logger，没有 `root` 项 |
| 起服务时 root 的状态 | `logging.getLogger()` → **`level=WARNING`, `handlers=[]`** |
| 我们 logger 的有效级别 | `app.tools.knowledge` → **WARNING**（自己没设，继承 root） |
| 我们打的 `logger.info(...)` | **被丢弃**，控制台什么都没有 |
| WARNING 以上 | 能出来，但走 `logging.lastResort`（一个 WARNING 级的**裸** stderr handler）——**只有消息本身**，没时间戳、没级别、没 logger 名 |

> 顺带解释一个容易看错的地方：uvicorn 自己的 `INFO: Application startup complete` 能打出来，
> **不是因为 root 有 handler**，而是因为 `uvicorn.error` 的父 logger 是 `uvicorn`（它有 `default`
> handler、级别 INFO）。两条路径完全独立——所以「uvicorn 的日志看得见」不代表我们的看得见。

## 决策

**1. 在 lifespan 里消费 `log_level`，而不是模块导入期。**

级别来自 `config.yaml`，得等配置加载完才知道。放在 `AppConfig.model_validate` 之后、那些会打 INFO
的启动步骤（技能投影 / MCP 装载 / 知识库）**之前**。

**2. 只配 `app` 与 `evaharness` 两个命名空间，**不用** `logging.basicConfig`。**

`basicConfig` 动的是 root，会把**第三方**的日志一起放开：httpx 每个请求一条 INFO、chromadb 也有
自己的一堆——很快就刷屏，真正想看的那几条反而淹了（本机实测 `basicConfig(INFO)` 下 httpx 的
`HTTP Request: POST …` 会持续输出）。

所以只对我们自己的顶层命名空间设级别、挂一个带格式的 handler，并设 **`propagate=False`**：子
logger（`app.tools.knowledge` 之类）继承它，别人的 logger 一个都不碰。`propagate=False` 是为了
「哪天 root 有了 handler，同一条日志不会被打两遍」。

**3. 级别认不出来**不抛**，回落到 INFO 并记一条警告。**

配置写错不该让服务起不来——与 ADR-0012 那些「工具写错就拒绝启动」不同：工具是能力边界，日志级别
只是个观感旋钮。但也不能静默：那条警告点名了写错的值。

**4. 幂等。**

重复调用只替换**我们自己挂上去的那个** handler（handler 上打了个记号），不会越挂越多、把同一条
日志打两遍。lifespan 在测试里会被跑很多遍，这条不是洁癖。

**5. 去处可注入（`stream=`）。**

默认 `sys.stderr`；测试传一个 `StringIO` 就能断言格式与过滤，不必去翻真 stderr。

## 后果

- **T18 那条「有日志可观察」在真实部署下成立了**：起一次真 uvicorn 就能看到
  `… INFO app.tools.knowledge: 知识库无变化（2 个文件、库内 7 个片段），跳过重建`。
- **INFO 一共 15 条左右有了归宿**：知识库的增量重建、MCP 装载与禁用、技能投影、checkpoint 与
  孤儿容器清理、会话删除。之前它们全是哑的。
- **`uvicorn --log-level` 与 `log_level` 是两个旋钮**：前者只管 uvicorn 自己的三个 logger，
  后者只管我们这两个命名空间。互不干扰，也互不覆盖。
- **跑 lifespan 的测试要把它换成空实现**（`test_gateway_app_state.py` 已加 autouse fixture）：
  真配置改的是**全局** logger，`propagate=False` 会切断 `caplog` 依赖的「冒泡到 root」，不隔开
  会让别的测试文件的 caplog 断言按执行顺序时对时错。

## 不做 / 范围外

- **不改 uvicorn 的日志配置**（`--log-config` / 自己的 `dictConfig`）：那是部署的事，不在本片。
- **不引结构化日志 / JSON 输出 / 日志轮转 / 按文件落盘**：单机个人助手，stderr 够用；RunJournal
  才是这套系统的「可观测载体」，日志只管运维。
- **不做「按 logger 名单独调级别」**：还没有第二个需要它的场景。
