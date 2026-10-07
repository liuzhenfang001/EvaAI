"use client";

import { useCallback, useEffect, useState } from "react";
import { API_URL } from "@/core/api/config";
import { parseSSE } from "@/core/api/stream";
import { applyFrame, emptyRunState, JournalEntry, RunState, toFrame } from "@/core/chat/runState";
import {
  cancelRun,
  deleteThread,
  fetchThreadJournal,
  listThreads,
  newThreadId,
  readCurrentThreadId,
  rememberCurrentThreadId,
  renameThread,
  ThreadSummary,
} from "@/core/chat/session";

export * from "@/core/chat/runState";
export type { ThreadSummary } from "@/core/chat/session";


/** 把一串事件喂给投影函数，返回重建好的状态——实时与回放共用这一条路径。 */
function project(entries: Iterable<{ event: unknown }>, from: RunState): RunState {
  let state = from;
  for (const entry of entries) {
    const frame = toFrame(entry.event as Parameters<typeof toFrame>[0]);
    state = applyFrame(state, frame.event, frame.data);
  }
  return state;
}

export function useChat() {
  const [state, setState] = useState<RunState>(emptyRunState);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [threads, setThreads] = useState<ThreadSummary[]>([]);

  const refreshThreads = useCallback(async () => {
    try {
      setThreads(await listThreads());
    } catch {
      // 会话列表拉不到不该打断聊天。
    }
  }, []);

  const openThread = useCallback(async (threadId: string) => {
    setError(null);
    try {
      const entries = await fetchThreadJournal(threadId);
      setState(project(entries, emptyRunState()));
      rememberCurrentThreadId(threadId);
    } catch (e) {
      // localStorage 里可能留着一个早已被删掉的 id——清掉它，回到新会话。
      rememberCurrentThreadId(null);
      setState(emptyRunState());
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const startNewThread = useCallback(() => {
    // 只换本地 id：会话由第一条消息跑起来时自然落进 journal，不需要先建实体。
    setState(emptyRunState());
    rememberCurrentThreadId(newThreadId());
    setError(null);
  }, []);

  useEffect(() => {
    void refreshThreads();
    const saved = readCurrentThreadId();
    if (saved) {
      void openThread(saved);
    } else {
      rememberCurrentThreadId(newThreadId());
    }
  }, [refreshThreads, openThread]);

  // run_start 会带回（或由服务端生成）会话 id——记住它，刷新后才接得上。
  useEffect(() => {
    if (state.threadId) {
      rememberCurrentThreadId(state.threadId);
    }
  }, [state.threadId]);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || streaming) return;
      setError(null);
      // 刻意**不**在这里塞用户气泡：run_start 事件会造它。这样实时与历史都由同一个
      // 事件产生同一条气泡，不会出现「刷新后长得不一样」。
      setState((prev) => ({
        ...emptyRunState(),
        threadId: prev.threadId,
        messages: prev.messages,
      }));
      setStreaming(true);

      try {
        const resp = await fetch(`${API_URL}/chat/stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: trimmed, thread_id: state.threadId }),
        });
        if (!resp.ok || !resp.body) {
          throw new Error(`请求失败：HTTP ${resp.status}`);
        }

        for await (const frame of parseSSE(resp.body)) {
          if (frame.event === "end") continue;
          if (frame.event === "error") {
            const detail = frame.data as { message?: string } | null;
            throw new Error(detail?.message ?? "未知错误");
          }
          setState((prev) => applyFrame(prev, frame.event, frame.data));
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setStreaming(false);
        void refreshThreads();
      }
    },
    [streaming, state.threadId, refreshThreads],
  );

  const cancel = useCallback(() => {
    // 只发取消请求，**不 abort fetch**：让 SSE 保持打开，后端经同一条流把
    // run_end(cancelled) 与 end 送回来，`send` 的 for-await 自然走到 finally 把
    // streaming 置 false。终态走同一条流，不搞前端乐观写入的第二条路径（ADR-0006）。
    if (!state.runId || !streaming) return;
    void cancelRun(state.runId).catch(() => {
      // 取消失败（例如 run 恰好已经自然结束）不影响 UI：真正的状态变化来自 SSE 终态。
    });
  }, [state.runId, streaming]);

  const rename = useCallback(
    async (threadId: string, title: string) => {
      try {
        await renameThread(threadId, title);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
      await refreshThreads();
    },
    [refreshThreads],
  );

  const remove = useCallback(
    async (threadId: string) => {
      try {
        await deleteThread(threadId);
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
      // 删掉的正是当前会话时就开一段新的，别让界面停在已经不存在的会话上。
      if (threadId === state.threadId) {
        startNewThread();
      }
      await refreshThreads();
    },
    [state.threadId, startNewThread, refreshThreads],
  );

  return {
    messages: state.messages,
    plan: state.plan,
    summary: state.summary,
    replan: state.replan,
    runId: state.runId,
    threadId: state.threadId,
    mode: state.mode,
    timeline: state.timeline,
    threads,
    streaming,
    error,
    send,
    cancel,
    openThread,
    startNewThread,
    rename,
    remove,
  };
}

export type { JournalEntry };
