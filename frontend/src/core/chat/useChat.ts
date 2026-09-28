"use client";

import { useCallback, useState } from "react";
import { parseSSE } from "@/core/api/stream";

export interface ToolTrace {
  label: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  tools: ToolTrace[];
}

export type TaskStatus = "pending" | "running" | "done" | "failed" | "skipped";

/** 计划面板里的一张任务卡（由 plan / task_start / task_end 事件驱动）。 */
export interface PlanTaskView {
  id: string;
  description: string;
  completionCriteria: string[];
  dependsOn: string[];
  status: TaskStatus;
  output?: string | null;
  error?: string | null;
}

export interface RunSummary {
  status: "completed" | "partial" | "stopped";
  failed: string[];
  skipped: string[];
}

/** replanner 的最近一次判定（「为什么剩下的任务没跑」的答案）。 */
export interface ReplanView {
  shouldContinue: boolean;
  reason: string;
}

interface ToolCallChunk {
  name?: string;
}

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8001";

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 面板语义是「当前这一次运行」，所以每次 send 开头重置。
  const [plan, setPlan] = useState<PlanTaskView[] | null>(null);
  const [summary, setSummary] = useState<RunSummary | null>(null);
  const [replan, setReplan] = useState<ReplanView | null>(null);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || streaming) return;
      setError(null);
      setSummary(null);
      setPlan(null);
      setReplan(null);
      setStreaming(true);
      setMessages((prev) => [
        ...prev,
        { role: "user", content: trimmed, tools: [] },
        { role: "assistant", content: "", tools: [] },
      ]);

      const patchAssistant = (fn: (m: ChatMessage) => ChatMessage) => {
        setMessages((prev) => {
          const copy = [...prev];
          const last = copy[copy.length - 1];
          if (last && last.role === "assistant") {
            copy[copy.length - 1] = fn(last);
          }
          return copy;
        });
      };

      const patchTask = (taskId: string, patch: Partial<PlanTaskView>) => {
        setPlan((prev) => (prev ? prev.map((t) => (t.id === taskId ? { ...t, ...patch } : t)) : prev));
      };

      let started = 0;

      try {
        const resp = await fetch(`${API_URL}/chat/stream`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: trimmed }),
        });
        if (!resp.ok || !resp.body) {
          throw new Error(`请求失败：HTTP ${resp.status}`);
        }

        for await (const { event, data } of parseSSE(resp.body)) {
          if (event === "messages") {
            const chunk = (data as unknown[])[0] as Record<string, unknown>;

            // 1. 文本增量（AIMessageChunk）
            const content = chunk.content;
            if (typeof content === "string" && content.length > 0 && chunk.type !== "tool") {
              patchAssistant((m) => ({ ...m, content: m.content + content }));
            }

            // 2. 工具调用（完整 tool_calls 或流式 tool_call_chunks）
            const toolCalls =
              Array.isArray(chunk.tool_calls) && chunk.tool_calls.length > 0
                ? (chunk.tool_calls as ToolCallChunk[])
                : Array.isArray(chunk.tool_call_chunks) && chunk.tool_call_chunks.length > 0
                  ? (chunk.tool_call_chunks as ToolCallChunk[])
                  : null;
            if (toolCalls) {
              for (const tc of toolCalls) {
                const name = tc.name ?? "工具";
                patchAssistant((m) => ({
                  ...m,
                  tools: [...m.tools, { label: `调用 ${name}` }],
                }));
              }
            }

            // 3. 工具结果（ToolMessage）
            if (chunk.type === "tool" && typeof chunk.name === "string") {
              patchAssistant((m) => ({
                ...m,
                tools: [...m.tools, { label: `${chunk.name} 完成` }],
              }));
            }
          } else if (event === "plan") {
            const tasks = (data as { tasks: RawTask[] }).tasks;
            setPlan(
              tasks.map((task) => ({
                id: task.id,
                description: task.description,
                completionCriteria: task.completion_criteria ?? [],
                dependsOn: task.depends_on ?? [],
                status: "pending" as TaskStatus,
              })),
            );
          } else if (event === "task_start") {
            const { task_id } = data as { task_id: string };
            patchTask(task_id, { status: "running" });
            // 多任务之间留空行；分隔符放前端，线上载荷保持干净。
            if (started > 0) {
              patchAssistant((m) => ({ ...m, content: m.content + "\n\n" }));
            }
            started += 1;
          } else if (event === "task_end") {
            const { task_id, status, output, error: taskError } = data as {
              task_id: string;
              status: TaskStatus;
              output?: string | null;
              error?: string | null;
            };
            patchTask(task_id, { status, output, error: taskError });
            // 失败必须在主对话里可见，否则「结果汇总返回用户」这条验收不成立。
            if (status === "failed") {
              patchAssistant((m) => ({
                ...m,
                content: `${m.content}\n\n[任务 ${task_id} 失败：${taskError ?? "未知原因"}]`,
              }));
            }
          } else if (event === "replan") {
            const payload = data as { should_continue: boolean; reason?: string };
            setReplan({ shouldContinue: payload.should_continue, reason: payload.reason ?? "" });
          } else if (event === "summary") {
            const payload = data as { status: RunSummary["status"]; failed: string[]; skipped: string[] };
            setSummary({ status: payload.status, failed: payload.failed ?? [], skipped: payload.skipped ?? [] });
          } else if (event === "error") {
            const err = data as { message?: string };
            throw new Error(err.message ?? "未知错误");
          }
          // event === "end" 时由 for-await 自然结束
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setStreaming(false);
      }
    },
    [streaming],
  );

  return { messages, streaming, error, plan, summary, replan, send };
}

interface RawTask {
  id: string;
  description: string;
  completion_criteria?: string[];
  depends_on?: string[];
}
