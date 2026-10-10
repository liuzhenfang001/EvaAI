# docker 沙箱：每会话一个长驻容器 + bash 工具

ADR-0012 定的软沙箱（虚拟路径 + 挂载表）**只解决了一半问题**：它让模型的路径不出界，但让模型
「不碰宿主」靠的是**我们自己**在词法层与解析后各拦一次——而那条路永远开不出 `bash` 工具。
ADR-0012 自己写着：「将来接真沙箱时，换的是这一层『挂载表怎么渲染成宿主路径』，工具签名不变」，
并把 bash 列在「不做」里，理由是「要它得先有真沙箱」。

本 ADR 记录那个真沙箱：新增 `bash` 工具，命令在 Linux 容器里跑。

## 决策

**1. 容器内路径 = 虚拟路径，四个挂载点原样 bind 进去。**

| 虚拟 = 容器内 | 宿主 | 容器里的权限 |
|---|---|---|
| `/workspace` | `.evaai/user/{会话}/workspace` | 读写 |
| `/output` | `.evaai/user/{会话}/output` | 读写 |
| `/input` | `.evaai/user/{会话}/input` | **只读（`:ro`）** |
| `/skills` | `.evaai/skills-view` | **只读（`:ro`）** |

这是本片的**全部意义**：模型仍然只面对一套坐标系，而文件工具与容器里跑的命令看到的是**同一份
文件**——不是靠约定对齐，是同一个目录（端到端测试双向验过：容器里写 `echo > /workspace/x`，
宿主 `.evaai/user/{会话}/workspace/x` 出现；反向也可读）。`/input` 与 `/skills` 的只读从
「我们自己遵守的约定」变成 **docker 强制的边界**——容器里 `touch /input/x` 会真的失败。

宿主路径仍由 `app/tools/sandbox.py` 独家渲染（`Sandbox.resolve()`），宿主布局的知识没有第二个
出处；docker 自己吐的报错也要过掩码（见 §7）。

**2. 不为容器再写一套文件操作。**

既然容器内路径就是虚拟路径，再实现一套「docker 内 ls/read/write」等于**两个坐标系、两个权威**，
哪天某个挂载点在两边漂了，模型会看到两个互相矛盾的世界。因而六个文件工具继续在宿主侧做 I/O，
只有 bash 进容器。

**3. 每会话一个长驻容器，命名由会话 id 推出，不做任何内存映射。**

容器名 `evaai-<会话>`（会话 id 的既有正则 `^[A-Za-z0-9_-]{1,64}$` 保证它是合法的 docker 名）。
于是**不需要** `dict[会话, 容器]`，也就没有 ADR-0012 警告过的那种无界字典。每次调用**先试
`exec`**，只有容器不可用（`No such container` / `is not running`）才建。并发首次调用靠 **docker 的
全局名字唯一性**当互斥锁：一个 `docker run` 赢，另一个撞名失败后直接复用赢家。

判据是 docker 的**报错标记**而不是退出码——退出码 127 是容器里合法的「命令未找到」。

长驻是为了让**容器内**的状态（装过的包、`/tmp`）在会话里留存。跨后端重启也复用：见 §6 的
deployment label。

**4. 超时的真边界是容器内的 `timeout -k 5 <n>`。**

杀掉 `docker exec` 客户端**杀不掉**容器里的进程（它归 dockerd 管）。所以每条命令都被包进
`timeout -k 5 <timeout> bash -c <命令>`——容器内那个用**配置里那个秒数**，于是模型看到的
「timed out after 60s」与配置里的 `timeout: 60` 是同一个数；客户端的超时取 `timeout + 10`，只是
daemon 卡死时的兜底。`-k 5` 不能省——命令无视 SIGTERM 的话，不带 `-k` 会永久挂住。超时表现为
退出码 124（`-k` 补刀则是 137），比一次不透明的 kill 有用得多。

**5. 用 `asyncio.to_thread` + 同步 `subprocess`，不用 `create_subprocess_exec`。**

Windows 上 `create_subprocess_exec` 要求 Proactor 事件循环，而 uvicorn 为了 Ctrl+C 可能跑在
Selector 上——那时是 `NotImplementedError`，而且只在真跑起来才暴露。丢进 worker 线程用同步 API，
事件循环类型就不再是前提，顺带省掉 `wait_for` + `kill` + `await wait` 一整套。

**6. 清理：启动期 reconcile + 删会话时连带删容器，都不拦启动。**

- 每个容器带三个 label：`evaai.managed=1`、`evaai.deployment=<SANDBOX_ROOT 的哈希>`、
  `evaai.session=<会话>`。
- **deployment 用稳定哈希，不用「每次启动生成的 uuid」**：uuid 会让启动期清理把上一轮留下的容器
  全删掉，也就毁掉了跨重启复用，而且同机第二个实例会删掉第一个的容器。
- 启动期列出本部署的容器，与 `journal_store.threads()` 比对，`rm -f` 掉**会话已不在 journal 里**的。
  判据**不是**「会话目录是否存在」——ADR-0012 记着「删会话不删文件」这条债，那个判据几乎永不命中。
  `threads()` 失败就整个跳过，**不退化成按时间删**：删掉一个还在用的容器比留几个孤儿糟得多。
- 整个 sweep 包 try/except → `logger.warning`，与 `_prune_checkpoints` 同一形态：**清理不该拦着
  服务起来**。
- `DELETE /threads/{id}` 连带删该会话容器（best-effort，失败只 warning，204 照常回）。容器是这个
  会话的**运行状态**，不是用户的文件——「删会话不删文件」那条债不适用于它。

**7. 命令自己的非零退出码是**结果**，不是失败。**

对 ADR-0012 §6「失败一律 raise」的**有意偏离**。那条针对的是「工具没能完成它的工作」；而 shell
命令的非零退出是**结果**，模型需要读到它的输出。两种形状在文本上分得开：docker 层失败走中间件的
`Error: ...`（`status="error"`），成功走输出 + `[exit code N]`。

`stderr` **合并**进 `stdout`：一个线程读两个管道会死锁（要上 selectors 或双线程），而终端本来就是
合并的。代价是拿不到「哪句是 stderr」，换来的是这段代码没有并发机关。

**8. 有界读，不整份缓冲。**

`yes` / `cat /dev/urandom` 在超时之前能吐几十 GB，`capture_output` 就是把服务 OOM 掉。所以用
`Popen` + 带上限的读循环（内部 256 KB），读满就停止读取并杀掉客户端。这一层被触发的退出码是我们
自己杀出来的，所以那一次**不报**退出码。

**9. 输出先掩码再截断。**

截断可能把宿主根劈成两半，之后再掩码就漏了那半截。掩码比 `Sandbox.mask` 宽：docker 的报错会吐
`\\?\D:\…`、`D:/…`（正斜杠）、小写盘符这类变体——ADR-0012 记过一次被路径写法咬到的实测
（`%TEMP%` 的 8.3 短名 `ADMINI~1`），这里宁宽勿窄。

截断**保留头也保留尾**（与 `file_ops` 只截头不同）：源码的价值在前面，命令报错的价值在**尾巴**上。

**10. 启动期校验四件事，任一不合格就拒绝启动。**

`shutil.which("docker")` → `docker info`（10 秒超时，daemon 挂着不能拖死启动）→
`docker image inspect <镜像>` → 一次性探针容器 `command -v bash timeout sleep`。

最后那步不能省，这是 ADR-0012 2b 的教训原样复用：**缺配置的失败模式要按实测写**。镜像里没有
`bash` 的话，症状是**模型第一次调用**时才炸——而那时离「你刚改了配置」已经很远。

**不自动 pull**：镜像可能好几个 GB，而且在启动期拉外网是个不该有的惊喜。失败信息给可执行的命令。

**11. 静默空挂载要变成响的。**

Docker Desktop 没共享某个盘符时，`-v D:/…` **不报错**——它会在容器里建一个**空目录**。模型看到
空的 `/workspace`，一头雾水，而且 `docker inspect` 也看不出区别（Mount 条目照样在）。

对策：启动期在 `SANDBOX_ROOT/.mount-check/` 放一个哨兵文件，以 `-v <它>:/evaai-check:ro` 挂进
容器（**第五个挂载，模型看不见**）；容器建好后探一次 `test -f /evaai-check/ok`，缺了就抛带
File Sharing 提示的错，并把那个坏容器删掉。约十行代码，把最坏的那种静默失败变成响的——与
ADR-0012 记「虚拟路径静默退化」那次的取向一致。

**12. 提示词里的 bash 段只在工具真的装了时才拼。**

在 `AgentFactory._prompt` 里拼（与技能清单同一处，T14 的挂载点），并守住那条既有契约：
**没有 bash 时逐字节原样返回**——否则没启用这个工具的部署会拿到一份不同的提示词，前缀缓存白废。

文案说清三件事：① bash 在本会话私有的容器里，不是用户的本机；② **同样是那四个虚拟目录**、
路径一模一样（这是消除「两套坐标系」疑虑的关键一句）；③ 四个目录**之外**的东西（`/tmp`、装的包、
后台进程）文件工具看不见、容器重建就没，所以**要留下的东西写到 `/workspace` 或 `/output`**；
另注「每次调用是全新 shell，`cd`/`export` 不跨调用」。

**13. 参数全在 tools 条目里，不新增 `AppConfig` 节。**

`AppConfig` 的 docstring 早就预告了 `sandbox` 这一节，但本片**没有**加：ADR-0012 §2 已经定过
「工具私有参数走工厂闭包，不读全局配置」，而这里需要的每个旋钮（镜像、网络、超时、内存、CPU、
进程数）都是**这个工具**的。新增一节反而会造出第二个读配置的入口。

`app/gateway/app.py` 与 `threads.py` 里那两处生命周期代码**不需要**这些参数：reconcile 与删容器
只认 label，不认镜像。所以只传一个布尔——`app.state.bash_enabled`。

## 镜像：为什么是它，以及我们盖掉了它什么

用的是 `enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:1.11.0`
（digest `sha256:6328d7fd2f0f…`）。实测：Ubuntu 22.04 底，`bash`、GNU `timeout`（支持 `-k`）、
`sleep infinity`、`python3`、`node`、`npm`、`git`、`pip`、`uv`、`apt-get`、`curl`、`wget` 都在，
容器内是 root。对模型来说这是个很肥的执行环境。

**它的 ENTRYPOINT 是 `/opt/gem/run.sh`**（`docker image inspect` 的 `Config.Entrypoint` 直接写着；
该脚本 `gem_init.sh` → `exec gem.sh` → `supervisord -c /opt/gem/supervisord.conf`）。实测
（`--entrypoint /bin/cat` 读 `/opt/gem/supervisord/*.conf`）那一套 supervisord 起来的是：

`tigervnc`(`Xvnc`) + `openbox` + `dbus` + `autocutsel` + `fcitx5`（**一整套 X 桌面**）、
`agent-browser` 与 `mcp-server-browser`（内嵌 Chromium + CDP 桥）、`jupyter`、`code-server`、
`nginx`、`gost`、`nodejs-repl-server-20/22/24`（三个常驻 Node REPL），以及 `python-server`
（`SANDBOX_SRV_PORT=8091`，它**自己的 HTTP shell API**）。

我们**覆盖**成 `--entrypoint sleep <镜像> infinity`：不覆盖的话，每个会话容器都会起一台带桌面的
机器加一台 Chromium（内存与启动时间都吃不消），而我们的设计是 `exec` 进去，不需要那套服务。

**代价如实记下：AIO 自带的服务栈不启动**，我们只把镜像当「文件系统 + 工具链」用。将来若要用
（比如让模型驱动内嵌浏览器），那是另一片——要么不覆盖 entrypoint、要么走它的 HTTP API。

同理，建容器时加 **`--no-healthcheck`**：镜像自带的 HEALTHCHECK 探的正是那两个我们不启动的服务
端口（`nc -z localhost 8091` / 9222）。不关掉的话，容器会**永远显示 unhealthy**——这是端到端跑
一次才发现的（`docker ps` 里 `Up 6 seconds (health: starting)`），看着像坏了，而且 docker 会一直
白跑那个探针。

## 后果

- **`/input` 与 `/skills` 的只读变成强制的**，不再是「工具自己守规矩」。
- **模型可以做真计算**：`pip install` / `apt install` / 跑脚本 / 编译。装过的包在会话里留存
  （容器长驻），但 Docker Desktop 重启后会丢——`--rm` 的语义是「容器寿命 = dockerd 的寿命」，
  只有四个挂载目录是持久的。这条写进了工具 docstring 与提示词。
- **`config.example.yaml` 里 bash 是注释掉的**：新机器按「快速开始」不装 Docker 也能起服务
  （T6 的可运行标准）。取消注释而 docker / 镜像不可用 → **拒绝启动**。有一条测试守着模板里
  bash 必须仍是注释状态，否则整个单测套件会去真调 `docker info`。
- **`app.state.bash_enabled`** 是 lifespan 新挂的一个布尔，`DELETE /threads/{id}` 靠它决定要不要
  收尾。
- **单测不依赖 docker**：机制层与工具层之间留了一个 `Runner` 协议，测试注入假实现；真 docker 的
  验证在 `tests/test_bash_docker_integration.py`（`skipif`，12 条）。

## 硬约束

- **工具实例跨请求共享**（ADR-0011），所以 `DockerCli` 与 `BashService` 上不许缓存任何请求级状态。
  容器靠**名字**发现，不靠内存——这是 §3 那个决定的另一半理由。
- **命令是单个 argv 元素**，全程不走 shell：模型的引号、管道、重定向交给容器里的 bash 解析。
  这也躲开了 Git Bash 的 MSYS 路径改写（在 Git Bash 里**手敲** `docker run -v /bin/sh` 会被改写成
  `C:/Program Files/Git/usr/bin/sh`，Python 直接传 argv 不受影响——踩过一次，记在这里免得有人
  「修」成 shell 调用）。
- **宿主路径给 `-v` 时转正斜杠**（`D:/a/b`）。整个 spec 是一个 argv 元素，所以不需要引号。

## 不做 / 范围外

- **把文件工具搬进容器**（§2 的理由）。
- **AIO 镜像自带的服务栈**（见上）。
- **容器资源热改、`docker compose`、在仓库里放 Dockerfile**——用现成镜像。
- **「删会话不删文件」那条债照旧**：本片只连带删容器，不碰用户文件。
- **同机多实例共享 daemon**：label 能隔离 reconcile，但容器名 `evaai-<会话>` 仍会撞。单用户单实例
  部署，记为限制。

## 遗留债（写在这里，别当它不存在）

- **这不是安全边界级的隔离**：容器内是 root、可联网、可 `apt install`；`/input` 里的原件理论上
  可被 `curl` 外送。防的是「模型跑坏宿主」，不是恶意多租户。要更硬就得关网（`network: none`）或
  上 microVM。
- **Linux 宿主上的属主**：容器以 root 写的文件可能让非 root 的后端读不了 `/output`，下载路由会
  失败。Windows + Docker Desktop 不受影响（有映射）。
- **镜像的 `TZ=Asia/Singapore`** 会带进容器，`bash date` 与 `get_current_time` 可能不一致。很小，
  暂不处理。
- **容器不吃 `--cpus` 的环境**：`cpus` / `memory` / `pids_limit` 都支持留空串表示不设，但默认值是
  按 Docker Desktop 能接受的写的。
