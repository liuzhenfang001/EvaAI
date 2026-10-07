"""文件下发：把会话 ``/output`` 里的成品发给浏览器。

这是「文件卡片」的另一半——卡片是前端画出来的，但**字节得从这里来**。在此之前整个后端
没有任何文件下发通道（没有静态挂载、没有下载接口），模型只能告诉用户「我写到哪了」。

与 ``threads.py`` 同前缀（``/threads``）但独立成文件：那个是 journal 回放的读接口，这里是
字节流，关注点不同。相同的前缀让 URL 保持**会话作用域**——「谁的文件」在路径里一眼可见。

**只放行 ``/output``**：路径前缀由路由自己拼，所以 ``/workspace`` ``/input`` ``/skills``
天然不可达。要放宽得改这里，而不是指望调用方守规矩。``/input`` 与 ``/skills`` 本来只读，
``/workspace`` 是工作台（草稿），都不该被当成「已验证可交付」的东西发出去。
"""

from __future__ import annotations

import mimetypes
from urllib.parse import quote

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.tools.sandbox import PathNotAllowedError, Sandbox

router = APIRouter(prefix="/threads", tags=["files"])

_ACTIVE_SUFFIXES = frozenset({".html", ".htm", ".xhtml", ".svg", ".xml"})
"""只许下载、不许在浏览器里直接打开的后缀。

共同点是**浏览器会执行里面的东西**：模型写出来的 ``.html`` 若能 inline 打开，就等于让它在
这个源上跑脚本（同源策略下它能读走本源的响应）。所以这些一律 ``attachment``。

判据用**后缀**而不是 ``mimetypes`` 的结果：后者在 Windows 上会去查注册表，同一份代码在不同
机器上可能给出不同答案——安全判断不能依赖一个会因为环境而变的输入。
"""


@router.get("/{thread_id}/files/{path:path}")
async def get_output_file(thread_id: str, path: str, download: bool = False) -> FileResponse:
    """把 ``/output/<path>`` 的文件发出去。

    ``path`` 是**相对 /output 的路径**（前端从事件的 ``/output/xxx`` 里摘掉前缀得到）。

    ``download=true`` 强制附件——卡片上的按钮写的就是「下载」，而对 ``.png`` 这类浏览器
    自己会渲染的类型，光靠 inline 点开只会新开一个标签页，看着不像下载。

    先查会话存在再建 :class:`Sandbox`：构造它会顺手建目录，而一次 404 的请求不该在磁盘上
    留下痕迹（见 :meth:`~app.tools.sandbox.Sandbox.exists`）。
    """
    if not Sandbox.exists(thread_id):
        raise HTTPException(status_code=404, detail=f"没有这个会话：{thread_id}")

    try:
        # 前缀在这里拼死，调用方给什么都出不了 /output。resolve 会做归一化与两层越界防护
        # （`..`、绝对路径、符号链接/junction 逃逸都在那儿被挡）。
        target = Sandbox(thread_id).resolve(f"/output/{path}")
    except PathNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"没有这个文件：{path}")

    mime, _encoding = mimetypes.guess_type(target.name)
    forced = download or target.suffix.lower() in _ACTIVE_SUFFIXES
    disposition = "attachment" if forced else "inline"
    return FileResponse(
        path=target,
        media_type=mime or "application/octet-stream",
        # filename* 用 RFC 5987 的编码：中文文件名直接写进头会变成乱码。
        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(target.name)}"},
    )


__all__ = ["router"]
