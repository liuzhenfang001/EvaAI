"use client";

import { useCallback, useEffect, useState } from "react";
import { parseSSE } from "@/core/api/stream";
import {
  applyFrame,
  emptyRunState,
  JournalEntry,
  RunRef,
  RunState,
  toFrame,
} from "@/core/chat/runState";

export * from "@/core/chat/runState";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8001";

export function useChat() {
  const [state, setState] = useState<RunState>(emptyRunState);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunRef[]>([]);
  /** 当前画面是不是从 RunJournal 回放出来的（用于提示「这不是实时对话」）。 */
  const [replayedFrom, setReplayedFrom] = useState<string | null>(null);

  const refreshRuns = useCallback(async () => {
    try {
      const resp = await fetch(`${API_URL}/runs`);
      if (!resp.ok) return;
      setRuns((await resp.json()) as RunRef[]);
    } catch {
      // 历史列表拉不到不该打断聊天。
    }
  }, []);

  useEffect(() => {
    void refreshRuns();
  }, [refreshRuns]);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || streaming) return;
      setError(null);
      setReplayedFrom(null);
      // 面板语义是「当前这一次运行」，所以每次 send 重置运行级状态；
      // 对话记录保留，新的一轮追加在后面。
      setState((prev) => ({
        ...emptyRunState(),
        messages: [...prev.messages, { role: "user", content: trimmed, tools: [] }],
      }));
      setStreaming(true);

      try {
        const resp = await fetch(`${API_URL}/chat/stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: trimmed }),
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
        void refreshRuns();
      }
    },
    [streaming, refreshRuns],
  );

  const replay = useCallback(async (runId: string) => {
    if (!runId) return;
    setError(null);
    try {
      const resp = await fetch(`${API_URL}/runs/${encodeURIComponent(runId)}/journal`);
      if (!resp.ok) {
        throw new Error(`拉取运行记录失败：HTTP ${resp.status}`);
      }
      const entries = (await resp.json()) as JournalEntry[];
      // 回放 = 把存下来的事件喂给与实时**同一个**投影函数。
      let next = emptyRunState();
      for (const entry of entries) {
        const frame = toFrame(entry.event);
        next = applyFrame(next, frame.event, frame.data);
      }
      setState(next);
      setReplayedFrom(runId);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  return {
    messages: state.messages,
    plan: state.plan,
    summary: state.summary,
    replan: state.replan,
    runId: state.runId,
    timeline: state.timeline,
    streaming,
    error,
    runs,
    replayedFrom,
    send,
    replay,
  };
}
