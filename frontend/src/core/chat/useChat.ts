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

interface ToolCallChunk {
  name?: string;
}

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8001";

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || streaming) return;
      setError(null);
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

  return { messages, streaming, error, send };
}
