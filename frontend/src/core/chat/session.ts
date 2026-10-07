/** 会话（thread）的前端半边：当前会话记在 localStorage，列表与增删改走 gateway。
 *
 * 会话**不是**在前端「创建」出来的实体——它由第一次运行落进 journal 时自然形成。
 * 所以「开启新对话」只是本地换一个 id，真正的持久化发生在第一条消息跑起来之后。
 */

import { API_URL } from "@/core/api/config";
import { JournalEntry } from "@/core/chat/runState";

const STORAGE_KEY = "evaai.currentThreadId";

export interface ThreadSummary {
  thread_id: string;
  title: string;
  updated_at: string;
  run_count: number;
  last_run_id: string | null;
  preview: string;
}

export function readCurrentThreadId(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(STORAGE_KEY);
}

export function rememberCurrentThreadId(threadId: string | null): void {
  if (typeof window === "undefined") return;
  if (threadId) {
    window.localStorage.setItem(STORAGE_KEY, threadId);
  } else {
    window.localStorage.removeItem(STORAGE_KEY);
  }
}

/** 本地生成会话 id。
 *
 * 服务端的语义是「给了就用，没给就生成」，所以前端可以先斩后奏——不必先请求建会话。
 * 注意 `crypto.randomUUID` 只在安全上下文里存在：用局域网 IP 以 http 访问时没有，
 * 所以要留一条退路（那种场景下后端那边其实会替我们生成，但本地生成更早拿到 id）。
 */
export function newThreadId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return Array.from({ length: 24 }, () => Math.floor(Math.random() * 16).toString(16)).join("");
}

async function readJson<T>(resp: Response, what: string): Promise<T> {
  if (!resp.ok) {
    throw new Error(`${what}失败：HTTP ${resp.status}`);
  }
  return (await resp.json()) as T;
}

export async function listThreads(): Promise<ThreadSummary[]> {
  const resp = await fetch(`${API_URL}/threads`);
  return readJson<ThreadSummary[]>(resp, "拉取会话列表");
}

export async function renameThread(threadId: string, title: string): Promise<void> {
  const resp = await fetch(`${API_URL}/threads/${encodeURIComponent(threadId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });
  await readJson<ThreadSummary>(resp, "重命名会话");
}

export async function deleteThread(threadId: string): Promise<void> {
  const resp = await fetch(`${API_URL}/threads/${encodeURIComponent(threadId)}`, { method: "DELETE" });
  if (!resp.ok) {
    throw new Error(`删除会话失败：HTTP ${resp.status}`);
  }
}

/** 一次拉全整段会话的完整事件，喂给与实时同一个 reducer 即可重建对话与面板。 */
export async function fetchThreadJournal(threadId: string): Promise<JournalEntry[]> {
  const resp = await fetch(`${API_URL}/threads/${encodeURIComponent(threadId)}/journal`);
  return readJson<JournalEntry[]>(resp, "拉取会话记录");
}

/** 取消一条正在跑的流（T5）。
 *
 * 结果**不**从这里回来——取消成功后，后端经同一条 SSE 流下发
 * ``run_end(status="cancelled")`` 与 ``end``，前端照常走到自然的收尾。这里只负责把
 * 请求发出去；未命中（run 已自然结束）后端回 404，调用方自行决定要不要理会。
 */
export async function cancelRun(runId: string): Promise<void> {
  const resp = await fetch(`${API_URL}/chat/cancel`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: runId }),
  });
  if (!resp.ok) {
    throw new Error(`取消失败：HTTP ${resp.status}`);
  }
}
