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

/** 轮内的一次工具调用。 */
export interface ToolCall {
  /**
   * 轮内序号，来自 `tool_call_chunks.index`。
   *
   * **不能拿数组下标代替**：下标是「第几次到达」，它才是「第几个调用」。并行的稳定
   * 顺序也靠它——结果乱序回来时，渲染顺序不该跟着乱。
   */
  index: number;
  /** 供应商的 call id，工具结果的匹配键。 */
  callId?: string;
  /** 工具名。首块给名字，后续块是 `""`/`null`——那些块**不能**覆盖它。 */
  name: string;
  /** 参数的增量拼接（JSON 文本），展开历史步骤时显示。 */
  args: string;
  status: "running" | "done";
}

/** 参数只用于展示，所以要有个上限：`write_file` 的参数里可能是整个文件，无上限地拼接会让
 *  状态和 DOM 都跟着涨。超了就不再接，末尾留一个省略号当作「被截断」的标记。 */
const ARGS_LIMIT = 2000;

/**
 * 一轮 = 模型的一次发言：一段文本 + 它在这一轮发起的工具调用。
 *
 * 与 LangGraph 的节点名无关（`model` 节点跑一次产出一轮）。这样定义的好处是**回放不需要
 * 任何额外信息**——journal 里存的 chunk 序列本身就够重建它。
 */
export interface Round {
  text: string;
  toolCalls: ToolCall[];
  /**
   * 收到了第一个工具结果——也就是「这一轮的发言结束了」。
   *
   * 模型在工具结果回来之前不可能再说下一句，所以**第一个**结果落地即可关闭本轮；并行的
   * 其它工具还没回来也不影响，那期间不会有新正文。关闭之后到达的正文必然属于下一轮。
   *
   * 这就是「一轮怎么结束」的全部答案：不靠 metadata、不靠 run_end、不靠超时。
   */
  closed: boolean;
}

export interface ChatMessage {
  role: "user" | "assistant";
  /**
   * 用户气泡的请求原文（由 `run_start` 造出）。**助手气泡恒为空字符串**。
   *
   * 助手要显示的是结构化的 `rounds`，最终回复由 `finalText` 推出来。上一版把「所有
   * 非 tool 文本拼成一坨 content」，同一份信息在正文与工具列表里各存一遍——那正是
   * 重复标签与「历史轮次无法折叠」共同的温床。
   */
  text: string;
  /** 助手气泡的多轮过程。用户气泡恒为空数组。 */
  rounds: Round[];
  /** 运行级 / 任务级的失败备注。不属于任何一轮，单独挂在气泡底部。 */
  note?: string;
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

/** 最终回复 = 最后一轮**且那一轮没有工具调用**。没有就返回空串，不编造。 */
export function finalText(message: ChatMessage): string {
  const last = message.rounds[message.rounds.length - 1];
  return last && last.toolCalls.length === 0 ? last.text : "";
}

/** 过程轮次 = 除最终回复那一轮以外的全部。 */
export function processRounds(message: ChatMessage): Round[] {
  const last = message.rounds[message.rounds.length - 1];
  return last && last.toolCalls.length === 0 ? message.rounds.slice(0, -1) : message.rounds;
}

/** 这个气泡有没有任何可显示的东西——占位符判据与空气泡守卫都用它。 */
export function hasOutput(message: ChatMessage): boolean {
  return message.rounds.length > 0 || Boolean(message.note);
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

interface RawToolCall {
  id?: unknown;
  name?: unknown;
  args?: unknown;
  /** 只有 `tool_call_chunks` 有：轮内序号。 */
  index?: unknown;
}

interface RawChunk {
  content?: unknown;
  type?: unknown;
  name?: unknown;
  /** ToolMessage 的归属键——工具结果回填时唯一可靠的匹配键。 */
  tool_call_id?: unknown;
  tool_calls?: RawToolCall[];
  tool_call_chunks?: RawToolCall[];
}

/** 线上字段一律当 unknown 收再收窄：后端改一个可选字段不该让前端编译不过。 */
function str(value: unknown): string {
  return typeof value === "string" ? value : "";
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
  return [...messages, { role: "assistant", text: "", rounds: [], key, taskId }];
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
    messages: [...state.messages, { role: "user", text: data.request, rounds: [] }],
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
 *  失败必须出现在对话里，不能只躺在时间线上。
 *
 *  写进独立的 `note` 而不是拼进正文：正文是**推导出来的**最终回复，往里拼接会把一段
 *  完整的回答截成半句（「失败备注」和「模型说了什么」是两件事）。 */
function appendRunFailure(state: RunState, error: string): ChatMessage[] {
  const note = `运行失败：${error}`;
  const prefix = `${state.runId ?? ""}:`;
  let index = -1;
  state.messages.forEach((message, i) => {
    if (message.role === "assistant" && message.key?.startsWith(prefix)) index = i;
  });
  if (index < 0) return state.messages;
  return state.messages.map((message, i) =>
    i === index
      ? { ...message, note: message.note ? `${message.note}\n${note}` : note }
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

/** 参数增量拼接，到 :data:`ARGS_LIMIT` 就封顶（超限后连拼接也停掉，别白烧 CPU）。 */
function appendArgs(current: string, delta: string): string {
  if (!delta) return current;
  if (current.length >= ARGS_LIMIT) return current;
  const merged = current + delta;
  return merged.length > ARGS_LIMIT ? `${merged.slice(0, ARGS_LIMIT)}…` : merged;
}

function newRound(): Round {
  return { text: "", toolCalls: [], closed: false };
}

/** 让末尾轮处于「可接收增量」的状态：没有轮、或上一轮已关闭，就新开一轮。 */
function openRound(rounds: Round[]): Round[] {
  const last = rounds[rounds.length - 1];
  return last && !last.closed ? rounds : [...rounds, newRound()];
}

function appendText(rounds: Round[], text: string): Round[] {
  const opened = openRound(rounds);
  const last = opened[opened.length - 1];
  const out = [...opened];
  out[out.length - 1] = { ...last, text: last.text + text };
  return out;
}

interface IncomingCall {
  index: number;
  callId?: string;
  name: string;
  args: string;
}

/**
 * 从一个 chunk 里读出工具调用的**增量**。
 *
 * 主源是 `tool_call_chunks`：它是原始视图、带 `index`，参数片解析失败时也照样逐块到达。
 * `tool_calls` 是 langchain 在**每个** chunk 上从它派生的（`AIMessageChunk.init_tool_calls`
 * 是个 `mode="after"` 的 validator），派生时 `name = chunk["name"] or ""`——**「调用 工具」
 * 与「调用 」这两个错标签，全部来自把派生视图当主源。** 它只保留两个用途：chunks 整体
 * 缺失时兜底，以及 chunks 里名字为空时补名。
 */
function readToolCalls(chunk: RawChunk): IncomingCall[] {
  const chunks = Array.isArray(chunk.tool_call_chunks) ? chunk.tool_call_chunks : [];
  const calls = Array.isArray(chunk.tool_calls) ? chunk.tool_calls : [];
  const found = new Map<number, IncomingCall>();

  chunks.forEach((part, position) => {
    const index = typeof part?.index === "number" ? part.index : position;
    found.set(index, {
      index,
      callId: str(part?.id) || undefined,
      name: str(part?.name),
      args: str(part?.args),
    });
  });

  if (found.size === 0) {
    // 没有 chunks（非流式供应商 / 极老的 journal）：拿不到 index，只能按位置排。
    // 这里的 `args` 是**对象**（不是增量字符串），拍平一下。
    calls.forEach((call, position) => {
      found.set(position, {
        index: position,
        callId: str(call?.id) || undefined,
        name: str(call?.name),
        args: call?.args === undefined ? "" : JSON.stringify(call.args),
      });
    });
  } else {
    // 有 chunks：`tool_calls` **只用来补名**，绝不碰参数。
    //
    // 它的 `args` 是「截至当前解析出来的对象」，不是增量——第一块 `args` 是空串时，
    // 它已经能派生出 `{}` 来。拿它回填再让第二块的 `"{}"` 拼上去，就得到 `{}{}` 这种
    // 看着像坏了的东西（真实流里就两块：`""` 然后 `"{}"`，正确结果是 `{}`）。
    calls.forEach((call, position) => {
      const entry = found.get(position);
      if (entry && !entry.name) entry.name = str(call?.name);
    });
  }

  return [...found.values()].sort((a, b) => a.index - b.index);
}

/**
 * 按 `index` **合并**进末尾轮（不是追加）。
 *
 * 名字用 `||` 而不是 `??` 合并：`??` 只认 null/undefined，空串会穿过去——「调用 」这个
 * 空白标签就是这么来的。返回 `announced`（名字从无到有的那几次调用），时间线只为它记一步，
 * 同一次调用的后续增量块不再重复记。
 */
function upsertToolCalls(rounds: Round[], incoming: IncomingCall[]): { rounds: Round[]; announced: string[] } {
  const opened = openRound(rounds);
  const last = opened[opened.length - 1];
  const byIndex = new Map(last.toolCalls.map((call) => [call.index, call]));
  const announced: string[] = [];

  for (const item of incoming) {
    const previous = byIndex.get(item.index);
    if (!previous) {
      const created: ToolCall = {
        index: item.index,
        callId: item.callId,
        name: item.name,
        // 走同一个封顶函数：第一块就超长（单块塞满整个文件）时也得截住。
        args: appendArgs("", item.args),
        status: "running",
      };
      byIndex.set(item.index, created);
      if (created.name) announced.push(created.name);
      continue;
    }
    const name = previous.name || item.name;
    byIndex.set(item.index, {
      ...previous,
      callId: previous.callId ?? item.callId,
      name,
      // 参数是**增量**：逐块拼接。到上限就不再接——否则 write_file 那种「参数里装着一整个
      // 文件」的调用会一路拼下去，状态与 DOM 都跟着涨，而历史步骤并不需要全文。
      args: appendArgs(previous.args, item.args),
    });
    if (!previous.name && name) announced.push(name);
  }

  const out = [...opened];
  out[out.length - 1] = { ...last, toolCalls: [...byIndex.values()].sort((a, b) => a.index - b.index) };
  return { rounds: out, announced };
}

/**
 * 把一条工具结果挂回它所属的那次调用。
 *
 * 归属优先按 `tool_call_id`，退回「第一个还在跑的调用」（老 journal，或结果先于调用到达）。
 * 两条都落空就原样返回——不制造假数据。**绝不新开一轮**：结果不是模型的发言，新开一轮
 * 会把它渲染成一次「思考」，还会让紧随其后的真正回复变成第三轮。
 */
function attachToolResult(rounds: Round[], callId: string, name: string): Round[] {
  const out = rounds.map((round) => ({ ...round, toolCalls: round.toolCalls.map((call) => ({ ...call })) }));
  let owner: Round | undefined;
  let hit: ToolCall | undefined;

  if (callId) {
    for (const round of out) {
      const match = round.toolCalls.find((call) => call.callId === callId);
      if (match) {
        owner = round;
        hit = match;
        break;
      }
    }
  }
  if (!hit) {
    for (const round of out) {
      const match = round.toolCalls.find((call) => call.status === "running");
      if (match) {
        owner = round;
        hit = match;
        break;
      }
    }
  }
  if (!hit || !owner) return rounds;

  hit.status = "done";
  if (!hit.name && name) hit.name = name; // ToolMessage 的名字是权威名
  owner.closed = true;
  return out;
}

function applyMessages(state: RunState, data: unknown): RunState {
  const [chunk, metadata] = (Array.isArray(data) ? data : [{}, {}]) as [RawChunk, Record<string, unknown>];
  const taskId = typeof metadata?.task_id === "string" ? metadata.task_id : "";
  const key = bubbleKey(state.runId, taskId);
  let messages = state.messages;
  let timeline = state.timeline;

  // 0. 工具结果**最先判**。ToolMessage 的 content 也是字符串——先走正文分支就会被当成
  //    模型的正文拼进气泡，「推理过程里混着工具原始输出」就是这么来的。
  if (chunk?.type === "tool") {
    const name = str(chunk.name);
    const callId = str(chunk.tool_call_id);
    messages = patchBubble(messages, key, taskId, (m) => ({
      ...m,
      rounds: attachToolResult(m.rounds, callId, name),
    }));
    timeline = pushStep(timeline, { taskId, kind: "result", text: `${name || "工具"} 返回结果` });
    return { ...state, messages, timeline };
  }

  // 1. 正文与工具调用都只认「模型自己的消息」。type 缺失按模型消息处理——那是更老的格式，
  //    当下的 chunk 一律带 type。
  const type = str(chunk?.type);
  if (type !== "" && type !== "AIMessageChunk" && type !== "AIMessage") {
    return state;
  }

  const text = str(chunk?.content);
  if (text) {
    messages = patchBubble(messages, key, taskId, (m) => ({ ...m, rounds: appendText(m.rounds, text) }));
  }

  const calls = readToolCalls(chunk);
  if (calls.length > 0) {
    let announced: string[] = [];
    messages = patchBubble(messages, key, taskId, (m) => {
      const merged = upsertToolCalls(m.rounds, calls);
      announced = merged.announced;
      return { ...m, rounds: merged.rounds };
    });
    // 只在「名字第一次出现」时记一步：一次调用一步，不再一个 chunk 一步。
    for (const name of announced) {
      timeline = pushStep(timeline, { taskId, kind: "tool", text: `🔧 调用 ${name}` });
    }
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
      ? patchBubble(state.messages, bubbleKey(state.runId, data.task_id), data.task_id, (m) => {
          const note = `任务 ${data.task_id} 失败：${data.error ?? "未知原因"}`;
          return { ...m, note: m.note ? `${m.note}\n${note}` : note };
        })
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
