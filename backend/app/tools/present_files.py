"""``present_files``：把 ``/output`` 下的成品显式呈现给用户。

**为什么是一个显式工具，而不是后端自动扫 ``/output``**：扫目录的话「什么该给用户看」由
目录内容决定，模型没有任何控制权（没法只挑一部分，也没法说「这个是草稿先别给」）；而
「呈现给用户」本来就是一个**意图**，该由模型表达。这也是参照项目 DeerFlow 的做法。

**为什么成功时没有返回值般的产物**：它不改 state、不写文件，只是往 langgraph 的 custom 流里
送一个载荷（见 :mod:`evaharness.agents.lead_agent.custom_events`），由 runner 翻成
``files_presented`` 事件。事件经 SSE 到前端画卡片、经 RunJournal 落盘，**刷新后卡片仍在**。

**为什么它在 app 而文件工具在 harness**：T17 之前这里的理由曾是「它依赖 app 层的 Sandbox，
而 boundary 测试禁止 harness import app」——沙箱搬进 harness 之后那条理由就到期了。现在留下
它的理由更实在：它是**交付语义**（产出一条 ``files_presented`` 事件、只放行 ``/output``），
而 :mod:`evaharness.sandbox.file_ops` 那六个是通用的读写。

**只放行 ``/output``**：``/workspace`` 是工作台（草稿与中间产物），改坏不可逆的 ``/input`` 与
``/skills`` 更是只读。把「交付物」与「干活的地方」分开是沙箱本来就定好的语义，这里只是沿用。

**失败一律 raise**，不返回 ``Error: ...`` 字符串——与 :mod:`evaharness.sandbox.file_ops` 同一条规矩
（见该模块 docstring 与 ADR-0012）。
"""

from __future__ import annotations

import mimetypes
from typing import Any

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from evaharness.agents.lead_agent import present_files_payload
from evaharness.agents.middlewares.context import sandbox_layout, session_id

PRESENT_FILES_NAME = "present_files"
"""工具名。**子 agent 的图里必须把它排除掉**——子 agent 走 ``ainvoke``、没有 custom 模式，
它的 ``stream_writer`` 是 no-op，调用会静默失效（不抛异常、卡片不出现）。见 ADR-0015。"""

_OUTPUT_MOUNT = "/output"
"""唯一允许呈现的挂载点。改这里等于改产品语义，别忘了同步 ``app/prompts.py``。"""


@tool(PRESENT_FILES_NAME, parse_docstring=True)
def present_files(runtime: ToolRuntime[Any], paths: list[str]) -> str:
    """Hand finished files to the user so they can view and download them.

    Only files already written under /output can be presented: /workspace is your scratch
    space and never reaches the user. Call this once, after everything is written — do not
    call it for drafts, and do not paste the file contents into your reply.

    Args:
        paths: Virtual paths of the deliverables, each under /output, for example
            ["/output/report.md", "/output/quicksort.go"].
    """
    if not paths:
        raise ValueError("no paths given — pass the /output files you want to hand over")

    box = sandbox_layout(runtime).session(session_id(runtime))
    files: list[dict[str, Any]] = []

    for path in paths:
        # mount_of 顺带做归一化（反斜杠、`..`、盘符、`~` 都在这里被挡掉），拿到的挂载点
        # 才是权威判断——写 `path.startswith("/output")` 会被 `/output-x/` 之类骗过去，
        # 而相对路径会被归一成 `/workspace/...`，正好在这里被拒（提示模型写全路径）。
        mount, _relative = box.mount_of(path)
        if mount.virtual != _OUTPUT_MOUNT:
            raise ValueError(
                f"only files under {_OUTPUT_MOUNT} can be presented, but {path!r} is not there. "
                f"Write the deliverable to {_OUTPUT_MOUNT} first — scratch files stay private."
            )

        # resolve 是第二层防护：符号链接与 Windows junction 逃逸在词法层看不出来，
        # 它解析之后再比一次包含性。
        target = box.resolve(path)
        if not target.exists():
            raise ValueError(f"{box.display(target)} does not exist yet — write it before presenting it.")
        if target.is_dir():
            raise ValueError(f"{box.display(target)} is a directory; present files, not directories.")

        mime, _encoding = mimetypes.guess_type(target.name)
        files.append(
            {
                # 事件里一律用虚拟路径：宿主路径不进事件、不进前端（见 sandbox 模块说明）。
                "path": box.display(target),
                "name": target.name,
                "size": target.stat().st_size,
                "mime": mime,
            }
        )

    runtime.stream_writer(present_files_payload(files))
    return "Presented to the user: " + ", ".join(str(file["path"]) for file in files)


__all__ = ["PRESENT_FILES_NAME", "present_files"]
