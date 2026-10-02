"""Checkpoint 持久化：把 agent 图的 state 落到 SQLite。

三种坐标，全部以 ``{会话}:`` 开头，因此删会话时一次前缀扫描就能清干净
（:func:`delete_session_checkpoints`）：

===============  ==========================  ==================================
坐标              形状                        生命周期
===============  ==========================  ==================================
lead agent       ``{会话}:lead``             **会话级、跨轮固定**——记忆的来源
子 agent         ``{会话}:{运行}:sub:{调用}``  运行级，跨轮不累积
Plan-Exec 任务    ``{会话}:{运行}:{任务}``     运行级（封存路径）
===============  ==========================  ==================================
"""

from evaharness.checkpoint.sqlite_saver import (
    checkpoint_timestamp_ms,
    delete_session_checkpoints,
    lead_thread_id,
    make_sqlite_checkpointer,
    prune_checkpoints,
    subagent_thread_id,
    task_thread_id,
)

__all__ = [
    "checkpoint_timestamp_ms",
    "delete_session_checkpoints",
    "lead_thread_id",
    "make_sqlite_checkpointer",
    "prune_checkpoints",
    "subagent_thread_id",
    "task_thread_id",
]
