"""`extensions_config.json` 的 typed 契约——**MCP server 的声明**。

三层配置里这一层落地得最晚：`load_extensions()` 从 T4 起就在读它，却一直没有消费者，ADR-0012
当年还给这件事写过理由——「现在把工具塞进去等于把一个半成品坐实成契约」。T13 是把这份契约坐实的
那一片，所以这里只落 `mcpServers`，`skills` / `middlewares` 两节仍然只是声明的占位。

**为什么两个类都用 `extra="forbid"`**（与 `app_config.py` 里那些 `extra="allow"` 的类相反）：
那些类的额外键**有归宿**——模型的其余键透传给构造函数、工具工厂会因为不认识的键抛 `TypeError`。
这里没有下游构造器接住它们，于是允许额外键等于让 `comand: npx.cmd` 这种拼写错误被**静默忽略**，
症状是「明明配了 server，却一个工具都没有」——正是本项目一贯拒绝的失败形状。宁可启动期直接指出来。

本模块只做**数据**：不 import fastmcp，也不建任何连接。把声明变成客户端是
:mod:`evaharness.mcp.client` 的事。
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import PurePath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)

Transport = Literal["stdio", "http"]
"""支持的 transport。`sse` 与 `websocket` 故意不在内（决策 #6）。

`sse` 已被 streamable-http 取代、协议层已弃用；`websocket` 需要我们自己维护另一条连接路径。
两者都是「等真有人需要时再加」的东西。
"""

DEFAULT_TIMEOUT = 60.0
"""读超时与握手超时（秒），两者共用。

**必须显式给**：fastmcp 的 ``Client`` 不传时 ``read_timeout_seconds`` 是 ``None``，
``_init_timeout`` 也是 ``None``——而 stdio 子进程若一直不响应，langchain#40472 记的行为是
**永久挂起**（那个 issue 至今 OPEN）。默认给足：首次 ``npx`` 要把包下下来（实测本机 30.9 秒）。
"""

_EXTENSION_FALLBACKS = (".cmd", ".bat", ".exe")


def resolve_stdio_command(command: str) -> str:
    """把配置里写的命令解析成宿主上**真能执行**的那个文件。

    存在的唯一理由是 Windows：实测本机 ``shutil.which("npx")`` 返回的是
    ``D:\\develop\\npx``——一个**无扩展名的 sh 脚本**（Git Bash 的 shim）。CreateProcess 找到它
    就报 ``WinError 193: %1 不是有效的 Win32 应用程序``，而那句话里没有一个字指向真正的原因。
    同一个目录里的 ``npx.cmd`` 才是能跑的，而 ``which`` 不会替我们挑它。

    规则很窄，避免自作主张：

    - 非 Windows：原样返回。
    - 解析到带扩展名的文件：原样返回（``python.EXE`` 这种情况）。
    - 解析到**无扩展名**的文件：按 ``.cmd`` / ``.bat`` / ``.exe`` 找一个同名的兄弟，找到就用它。
    - ``which`` 什么也没找到：原样返回，让 spawn 自己失败——不替用户编路径。
    """

    if os.name != "nt":
        return command
    found = shutil.which(command)
    if not found or PurePath(found).suffix:
        return found or command
    for extension in _EXTENSION_FALLBACKS:
        candidate = f"{found}{extension}"
        if os.path.isfile(candidate):
            logger.debug("stdio 命令 %r 解析为 %r（原始解析结果不可执行）", command, candidate)
            return candidate
    return found


class McpServerConfig(BaseModel):
    """一个 MCP server 的声明。两种 transport 共用一个模型，靠 ``transport`` 分流。

    ``transport`` 可以省略——有 ``command`` 就是 stdio、有 ``url`` 就是 http。写自动推断是为了
    让「从别处抄来的一段 mcpServers」多半能直接用；但两个都给了就会在下面被拒（那多半是抄错了）。
    """

    model_config = ConfigDict(extra="forbid")

    transport: Transport | None = None

    # stdio
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    cwd: str | None = None

    # http
    url: str | None = None
    headers: dict[str, str] | None = None

    timeout: float = DEFAULT_TIMEOUT
    """读超时 / 握手超时，见 :data:`DEFAULT_TIMEOUT`。"""

    enabled: bool = True
    """这个 server 要不要启用。**缺省开**。

    关掉它 = **根本不建连**：不是「连上再把工具藏起来」。这一点是它存在的理由——启用的 server 要付
    启动开销（stdio 会 spawn 子进程，首次 `npx` 实测 30.9 秒），所以「这段配置先留着、但今天不
    用它」必须是不花钱的。想彻底不留痕迹就把整段删掉；这个开关服务的是「留着、临时别连」。
    """

    @model_validator(mode="before")
    @classmethod
    def _infer_transport(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("transport"):
            if data.get("command"):
                return {**data, "transport": "stdio"}
            if data.get("url"):
                return {**data, "transport": "http"}
        return data

    @model_validator(mode="after")
    def _check_required_fields(self) -> McpServerConfig:
        if self.transport is None:
            raise ValueError(
                "既没有 `command`（stdio）也没有 `url`（http），无法判断这个 server 怎么连"
            )
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("transport=stdio 需要 `command`（要跑什么命令，例如 npx.cmd）")
            if self.url:
                raise ValueError("transport=stdio 不该给 `url`（两个都给多半是抄错了）")
        else:
            if not self.url:
                raise ValueError("transport=http 需要 `url`")
            if self.command:
                raise ValueError("transport=http 不该给 `command`（两个都给多半是抄错了）")
        return self


class ExtensionsConfig(BaseModel):
    """`extensions_config.json` 的顶层形状。

    只有 ``mcp_servers`` 有消费者；``skills`` / ``middlewares`` 是**声明出来的占位**——声明它们
    是为了让 `extra="forbid"` 能生效（不然那两个键会被当成拼写错误）。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")
    skills: dict[str, Any] = Field(default_factory=dict)
    middlewares: list[Any] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _drop_broken_servers(cls, data: Any) -> Any:
        """坏的 server 在这里就被摘掉，**不让它拖垮整份文件**（决策 #2 的同一取向）。

        摘除 + 记日志只发生在 :func:`parse_mcp_servers` 一处——`model_validate` 拿到的已经是干净的
        映射，所以这里不会因为某个 server 写错而整体抛。
        """
        if not isinstance(data, dict):
            return data
        for key in ("mcpServers", "mcp_servers"):
            raw = data.get(key)
            if isinstance(raw, dict):
                return {**data, key: parse_mcp_servers(raw)}
        return data


def parse_mcp_servers(raw: dict[str, Any]) -> dict[str, McpServerConfig]:
    """逐个校验 server 声明，**跳过坏的并记警告**。

    为什么不是全有或全无：一份 `extensions_config.json` 里通常有多个 server，其中一个写错不该让
    其余的也失效——与「一个 server 连不上不影响别的」（决策 #2）是同一条取向。警告里带 server 名
    与 pydantic 的原始消息，所以跳过不是静默的。
    """

    servers: dict[str, McpServerConfig] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            logger.warning("MCP server %r 的声明不是对象，已跳过：%r", name, entry)
            continue
        try:
            servers[name] = McpServerConfig.model_validate(entry)
        except ValueError as exc:
            logger.warning("MCP server %r 的声明无效，已跳过：%s", name, exc)
    return servers


__all__ = [
    "DEFAULT_TIMEOUT",
    "ExtensionsConfig",
    "McpServerConfig",
    "Transport",
    "parse_mcp_servers",
    "resolve_stdio_command",
]
