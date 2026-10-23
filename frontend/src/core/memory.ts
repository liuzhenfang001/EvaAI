/** 长期记忆的前端半边：读 / 增 / 改 / 删，走 gateway 的 `/memory` 一组路由。
 *
 * 与 `core/chat/session.ts` 同一写法（同一处 `API_URL`、同样的 `readJson` 取向），
 * 差别只有一处：这里的错误**要把后端的 `detail` 带出来**。记忆的失败大多是「你给的这条
 * 内容已经记过了」这类可解释的冲突，只报一个 HTTP 码会让用户不知道该改什么。
 */

import { API_URL } from "@/core/api/config";

export interface MemoryFact {
  id: string;
  content: string;
  category: string;
  confidence: number;
  status: string;
  source_thread: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProfileBlock {
  section: string;
  summary: string;
  updated_at: string;
}

export interface MemorySnapshot {
  profile: ProfileBlock[];
  facts: MemoryFact[];
  /** 后端认的分类清单——下拉框按它渲染，不在前端另抄一份（抄了就会漂）。 */
  categories: string[];
}

export interface MemoryStatus {
  enabled: boolean;
  facts: number;
  profile_blocks: number;
}

/** 六块画像的中文名。取值与后端 `ProfileSection` 一一对应；缺失的块**不显示**。 */
export const SECTION_LABELS: Record<string, string> = {
  work_context: "工作",
  personal_context: "个人",
  top_of_mind: "当前关注",
  recent_months: "最近几个月",
  earlier_context: "更早的上下文",
  long_term_background: "长期背景",
};

export const CATEGORY_LABELS: Record<string, string> = {
  preference: "偏好",
  correction: "纠正",
  identity: "身份",
  goal: "目标",
  constraint: "约束",
  decision: "决定",
  behavior: "习惯",
  context: "处境",
  other: "其它",
};

async function fail(resp: Response, what: string): Promise<never> {
  let detail = "";
  try {
    const body = (await resp.json()) as { detail?: unknown };
    if (typeof body?.detail === "string") detail = body.detail;
  } catch {
    // 后端没回 JSON（网络层错误之类）——那就只有状态码可说。
  }
  throw new Error(detail ? `${what}失败：${detail}` : `${what}失败：HTTP ${resp.status}`);
}

async function readJson<T>(resp: Response, what: string): Promise<T> {
  if (!resp.ok) await fail(resp, what);
  return (await resp.json()) as T;
}

export async function loadMemory(): Promise<MemorySnapshot> {
  return readJson<MemorySnapshot>(await fetch(`${API_URL}/memory`), "读取记忆");
}

/** 这个部署开没开长期记忆。**没开时这个端点也答 200**——页面正是靠它决定要不要显示提示。 */
export async function loadMemoryStatus(): Promise<MemoryStatus> {
  return readJson<MemoryStatus>(await fetch(`${API_URL}/memory/status`), "读取记忆状态");
}

export interface FactInput {
  content: string;
  category: string;
  confidence?: number;
}

export async function createFact(fact: FactInput): Promise<MemoryFact> {
  const resp = await fetch(`${API_URL}/memory/facts`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fact),
  });
  return readJson<MemoryFact>(resp, "记住这条");
}

export async function updateFact(
  factId: string,
  patch: Partial<FactInput>,
): Promise<MemoryFact> {
  const resp = await fetch(`${API_URL}/memory/facts/${encodeURIComponent(factId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  return readJson<MemoryFact>(resp, "修改这条");
}

export async function deleteFact(factId: string): Promise<void> {
  const resp = await fetch(`${API_URL}/memory/facts/${encodeURIComponent(factId)}`, {
    method: "DELETE",
  });
  if (!resp.ok) await fail(resp, "删除这条");
}

/** 清空全部记忆。**不可逆**——调用方必须先让用户确认。 */
export async function clearMemory(): Promise<void> {
  const resp = await fetch(`${API_URL}/memory`, { method: "DELETE" });
  if (!resp.ok) await fail(resp, "清空记忆");
}

/** 时间戳渲染成「本地时间」。后端给的是 ISO 8601，`Invalid Date` 会显示成 `—` 而不是 NaN。 */
export function formatTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}
