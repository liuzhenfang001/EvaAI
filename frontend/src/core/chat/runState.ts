/** 一次运行的前端状态，以及「事件 → 状态」的纯投影。
 *
 * 抽成纯函数的目的只有一个：**实时与回放走同一个投影**。回放若另写一套，
 * 两边必然漂移——而那正是「可回放」最容易变成谎话的地方。
 *
 * 线上有两套 data 形状（后端 app/gateway/sse.py 有意保留）：`messages` 帧是
 * `[chunk, metadata]` 数组以兼容 T1，其余是带 `type` 判别键的对象。回放时
 * `toFrame` 会把 journal 里的扁平事件还原成同样的形状，于是 applyFrame 不必
 * 知道自己吃的是实时帧还是回放帧。
 */

export type TaskStatus = "pending" | "running" | "done" | "failed" | "skipped";

/** 计划面板里的一张任务卡。 */
export interface PlanTaskView {
  id: string;
  description: string;
  completionCriteria: string[];
  dependsOn: string[];
  status: TaskStatus;
  output: string | null;
  error: string | null;
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

/** 时间线一步的种类。`run` 是运行级的（计划 / 复核 / 汇总），其余属于某个任务。 */
export type TimelineKind = "run" | "start" | "tool" | "result" | "end";

export interface TimelineStep {
  seq: number;
  /** 空字符串表示这是运行级的一步，不属于任何任务。 */
  taskId: string;
  kind: TimelineKind;
  text: string;
}

export interface ToolTrace {
  label: string;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  tools: ToolTrace[];
  /**
   * 气泡的**唯一身份**：`runId:taskId`。
   *
   * 必须带上 run——任务 id 只在一次运行内唯一，同一个会话的第二轮又会有 `t1`。
   * 只按 `taskId` 认气泡的话，第二轮的输出会被追加进第一轮那个气泡里（看起来就是
   * 「上一轮的回答被覆盖了」）。
   */
  key?: string;
  /** 展示用的泳道标签（`t1` / `t2`）。**会跨运行重复，不能当身份用。** */
  taskId?: string;
}

/**
 * 这一轮走的是哪条路径，由 `run_start` 下发。
 *
 * `react` = ReAct 主 Agent 直答（T8 起的默认路径，没有计划）；`plan` = 封存的
 * Plan-Exec 编排。它决定气泡署名、面板文案与「正在思考」占位，**不能靠有没有
 * `plan` 事件反推**——回放中途的状态会让那种推断时对时错。
 */
export type RunMode = "react" | "plan";

export interface RunState {
  messages: ChatMessage[];
  plan: PlanTaskView[] | null;
  summary: RunSummary | null;
  replan: ReplanView | null;
  /** 本次运行的标识；由 `run_start` 下发。 */
  runId: string | null;
  /** 本段对话所属的会话；由 `run_start` 下发（服务端也可能替我们生成）。 */
  threadId: string | null;
  /** 本轮走的路径；由 `run_start` 下发。 */
  mode: RunMode | null;
  timeline: TimelineStep[];
}

export interface RunRef {
  run_id: string;
  updated_at: string;
}

export interface JournalEvent {
  type: string;
  [key: string]: unknown;
}

export interface JournalEntry {
  seq: number;
  event: JournalEvent;
}

export const emptyRunState = (): RunState => ({
  messages: [],
  plan: null,
  summary: null,
  replan: null,
  runId: null,
  threadId: null,
  mode: null,
  timeline: [],
});

// --------------------------------------------------------------------------- #
// 线上载荷
// --------------------------------------------------------------------------- #

interface RawRunStart {
  run_id: string;
  thread_id: string;
  request: string;
  /**
   * 缺省 `plan` 是**历史回退**而不是语义缺省：T8 之前写下的 journal 行里没有这个
   * 字段，而那些行确实都是 Plan-Exec 写的。写成 `react` 会让旧会话整段渲染错。
   */
  mode?: RunMode;
}

interface RawRunEnd {
  status: "completed" | "failed";
  output?: string | null;
  error?: string | null;
}

interface RawPlan {
  run_id: string;
  tasks: { id: string; description: string; completion_criteria?: string[]; depends_on?: string[] }[];
}

interface RawTaskStart {
  task_id: string;
}

interface RawTaskEnd {
  task_id: string;
  status: TaskStatus;
  output?: string | null;
  error?: string | null;
}

interface RawReplan {
  should_continue: boolean;
  reason?: string;
}

interface RawSummary {
  status: RunSummary["status"];
  failed?: string[];
  skipped?: string[];
}

interface RawChunk {
  content?: unknown;
  type?: unknown;
  name?: unknown;
  tool_calls?: { name?: string }[];
  tool_call_chunks?: { name?: string }[];
}

// --------------------------------------------------------------------------- #
// 投影
// --------------------------------------------------------------------------- #

export function applyFrame(state: RunState, name: string, data: unknown): RunState {
  switch (name) {
    case "run_start":
      return applyRunStart(state, data as RawRunStart);
    case "plan":
      return applyPlan(state, data as RawPlan);
    case "task_start":
      return applyTaskStart(state, data as RawTaskStart);
    case "messages":
      return applyMessages(state, data);
    case "task_end":
      return applyTaskEnd(state, data as RawTaskEnd);
    case "replan":
      return applyReplan(state, data as RawReplan);
    case "summary":
      return applySummary(state, data as RawSummary);
    case "run_end":
      return applyRunEnd(state, data as RawRunEnd);
    default:
      // 未知事件名直接忽略：新增事件不该让旧前端崩掉。
      return state;
  }
}

/** 把 journal 里的扁平事件还原成与实时帧相同的 (事件名, data) 形状。 */
export function toFrame(event: JournalEvent): { event: string; data: unknown } {
  if (event.type === "messages") {
    const metadata = (event.metadata ?? {}) as Record<string, unknown>;
    return {
      event: "messages",
      data: [event.chunk, { ...metadata, task_id: event.task_id }],
    };
  }
  return { event: event.type, data: event };
}

function pushStep(timeline: TimelineStep[], step: Omit<TimelineStep, "seq">): TimelineStep[] {
  // 步骤只增不改，所以 seq 直接用长度推导，不必额外维护计数器。
  return [...timeline, { seq: timeline.length + 1, ...step }];
}

/** 气泡身份：任务 id 只在一次运行内唯一，所以必须带上 run。 */
function bubbleKey(runId: string | null, taskId: string): string {
  return `${runId ?? ""}:${taskId}`;
}

function ensureBubble(messages: ChatMessage[], key: string, taskId: string): ChatMessage[] {
  if (!taskId || messages.some((m) => m.role === "assistant" && m.key === key)) {
    return messages;
  }
  return [...messages, { role: "assistant", content: "", tools: [], key, taskId }];
}

function patchBubble(
  messages: ChatMessage[],
  key: string,
  taskId: string,
  fn: (message: ChatMessage) => ChatMessage,
): ChatMessage[] {
  return ensureBubble(messages, key, taskId).map((m) =>
    m.role === "assistant" && m.key === key ? fn(m) : m,
  );
}

function applyRunStart(state: RunState, data: RawRunStart): RunState {
  // 新的一次运行：运行级状态（计划 / 时间线 / 汇总 / 复核）整体重置，只把对话接上。
  // 用户气泡**由事件产生**而不是 send 时自己塞——这是「历史与实时逐字一致」的关键：
  // 两条路径都由同一个 run_start 造出这条气泡。
  return {
    ...emptyRunState(),
    threadId: data.thread_id,
    runId: data.run_id,
    mode: data.mode ?? "plan",
    messages: [...state.messages, { role: "user", content: data.request, tools: [] }],
  };
}

function applyRunEnd(state: RunState, data: RawRunEnd): RunState {
  const failed = data.status === "failed";
  return {
    ...state,
    messages: failed ? appendRunFailure(state, data.error ?? "未知原因") : state.messages,
    timeline: pushStep(state.timeline, {
      taskId: "",
      kind: "end",
      text: failed ? `运行失败：${data.error ?? "未知原因"}` : "本轮完成",
    }),
  };
}

/** 把运行级失败写进这一轮的气泡——与 `applyTaskEnd` 对失败的处理对称：
 *  失败必须出现在对话里，不能只躺在时间线上。 */
function appendRunFailure(state: RunState, error: string): ChatMessage[] {
  const note = `[运行失败：${error}]`;
  const prefix = `${state.runId ?? ""}:`;
  let index = -1;
  state.messages.forEach((message, i) => {
    if (message.role === "assistant" && message.key?.startsWith(prefix)) index = i;
  });
  if (index < 0) return state.messages;
  return state.messages.map((message, i) =>
    i === index
      ? { ...message, content: message.content ? `${message.content}\n\n${note}` : note }
      : message,
  );
}

function applyPlan(state: RunState, data: RawPlan): RunState {
  return {
    ...state,
    plan: data.tasks.map((task) => ({
      id: task.id,
      description: task.description,
      completionCriteria: task.completion_criteria ?? [],
      dependsOn: task.depends_on ?? [],
      status: "pending" as TaskStatus,
      output: null,
      error: null,
    })),
    timeline: pushStep(state.timeline, {
      taskId: "",
      kind: "run",
      text: `计划：${data.tasks.length} 个任务`,
    }),
  };
}

function applyTaskStart(state: RunState, data: RawTaskStart): RunState {
  return {
    ...state,
    messages: ensureBubble(state.messages, bubbleKey(state.runId, data.task_id), data.task_id),
    timeline: pushStep(state.timeline, { taskId: data.task_id, kind: "start", text: "开始执行" }),
  };
}

function applyMessages(state: RunState, data: unknown): RunState {
  const [chunk, metadata] = (Array.isArray(data) ? data : [{}, {}]) as [RawChunk, Record<string, unknown>];
  const taskId = typeof metadata?.task_id === "string" ? metadata.task_id : "";
  const key = bubbleKey(state.runId, taskId);
  let messages = state.messages;
  let timeline = state.timeline;

  // 1. 文本增量（AIMessageChunk）
  const content = chunk?.content;
  if (typeof content === "string" && content.length > 0 && chunk.type !== "tool") {
    messages = patchBubble(messages, key, taskId, (m) => ({ ...m, content: m.content + content }));
  }

  // 2. 工具调用（完整 tool_calls 或流式 tool_call_chunks）
  const toolCalls =
    Array.isArray(chunk?.tool_calls) && chunk.tool_calls.length > 0
      ? chunk.tool_calls
      : Array.isArray(chunk?.tool_call_chunks) && chunk.tool_call_chunks.length > 0
        ? chunk.tool_call_chunks
        : null;
  if (toolCalls) {
    for (const call of toolCalls) {
      const label = call.name ?? "工具";
      messages = patchBubble(messages, key, taskId, (m) => ({ ...m, tools: [...m.tools, { label: `调用 ${label}` }] }));
      timeline = pushStep(timeline, { taskId, kind: "tool", text: `🔧 调用 ${label}` });
    }
  }

  // 3. 工具结果（ToolMessage）
  if (chunk?.type === "tool" && typeof chunk.name === "string") {
    messages = patchBubble(messages, key, taskId, (m) => ({ ...m, tools: [...m.tools, { label: `${chunk.name} 完成` }] }));
    timeline = pushStep(timeline, { taskId, kind: "result", text: `${chunk.name} 返回结果` });
  }

  return { ...state, messages, timeline };
}

function applyTaskEnd(state: RunState, data: RawTaskEnd): RunState {
  const plan =
    state.plan?.map((task) =>
      task.id === data.task_id
        ? { ...task, status: data.status, output: data.output ?? null, error: data.error ?? null }
        : task,
    ) ?? null;

  const text =
    data.status === "failed"
      ? `失败：${data.error ?? "未知原因"}`
      : data.status === "skipped"
        ? "已跳过"
        : "完成";
  const timeline = pushStep(state.timeline, { taskId: data.task_id, kind: "end", text });

  // 失败必须在主对话里可见，否则「结果汇总返回用户」这条验收不成立。
  const messages =
    data.status === "failed"
      ? patchBubble(state.messages, bubbleKey(state.runId, data.task_id), data.task_id, (m) => ({
          ...m,
          content: `${m.content}${m.content ? "\n\n" : ""}[任务 ${data.task_id} 失败：${data.error ?? "未知原因"}]`,
        }))
      : state.messages;

  return { ...state, plan, timeline, messages };
}

function applyReplan(state: RunState, data: RawReplan): RunState {
  const reason = data.reason ? `（${data.reason}）` : "";
  return {
    ...state,
    replan: { shouldContinue: data.should_continue, reason: data.reason ?? "" },
    timeline: pushStep(state.timeline, {
      taskId: "",
      kind: "run",
      text: `复核：${data.should_continue ? "继续" : "结束"}${reason}`,
    }),
  };
}

const SUMMARY_LABEL: Record<RunSummary["status"], string> = {
  completed: "全部完成",
  partial: "部分完成",
  stopped: "已提前结束",
};

function applySummary(state: RunState, data: RawSummary): RunState {
  return {
    ...state,
    summary: { status: data.status, failed: data.failed ?? [], skipped: data.skipped ?? [] },
    timeline: pushStep(state.timeline, {
      taskId: "",
      kind: "run",
      text: `汇总：${SUMMARY_LABEL[data.status] ?? data.status}`,
    }),
  };
}
