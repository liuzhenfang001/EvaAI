"use client";

import { useEffect, useRef, useState } from "react";
import {
  ChatMessage,
  finalText,
  hasOutput,
  PlanTaskView,
  processRounds,
  ReplanView,
  Round,
  RunMode,
  RunSummary,
  TaskStatus,
  ThreadSummary,
  TimelineKind,
  TimelineStep,
  useChat,
} from "@/core/chat/useChat";
import Markdown from "./Markdown";

/**
 * 右侧任务编排面板暂时下线：折叠式对话已经承载了「过程可见性」，而它是绝对定位的浮动
 * 覆盖层，窄屏会直接压住消息。回放时间线要回来，所以组件与样式原样保留，**只由这一个
 * 开关控制**——别再散落条件判断。
 */
const SHOW_TASK_PANEL = false;

const STATUS_LABEL: Record<TaskStatus, string> = {
  pending: "待执行",
  running: "执行中",
  done: "已完成",
  failed: "失败",
  skipped: "已跳过",
};

const STEP_ICON: Record<TimelineKind, string> = {
  run: "▸",
  start: "▶",
  tool: "🔧",
  result: "↩",
  end: "■",
};

const EMPTY_STEP: TimelineStep[] = [];

// --------------------------------------------------------------------------- #
// 左侧：会话列表
// --------------------------------------------------------------------------- #

function SessionList({
  threads,
  currentId,
  busy,
  onOpen,
  onNew,
  onRename,
  onRemove,
}: {
  threads: ThreadSummary[];
  currentId: string | null;
  busy: boolean;
  onOpen: (threadId: string) => void;
  onNew: () => void;
  onRename: (thread: ThreadSummary) => void;
  onRemove: (thread: ThreadSummary) => void;
}) {
  return (
    <aside className="sessions">
      <button className="new-thread" onClick={onNew} disabled={busy}>
        ＋ 开启新对话
      </button>

      {threads.length === 0 && <div className="sessions-empty">还没有历史对话</div>}

      <ul className="thread-list">
        {threads.map((thread) => (
          <li key={thread.thread_id} className={`thread-item ${thread.thread_id === currentId ? "active" : ""}`}>
            <button className="thread-open" onClick={() => onOpen(thread.thread_id)} disabled={busy}>
              <span className="thread-title">{thread.title}</span>
              {thread.preview && <span className="thread-preview">{thread.preview}</span>}
            </button>
            <span className="thread-actions">
              <button title="重命名" onClick={() => onRename(thread)} disabled={busy}>
                ✎
              </button>
              <button title="删除" onClick={() => onRemove(thread)} disabled={busy}>
                🗑
              </button>
            </span>
          </li>
        ))}
      </ul>
    </aside>
  );
}

// --------------------------------------------------------------------------- #
// 右侧：任务编排（浮动覆盖层，可收起）
// --------------------------------------------------------------------------- #

function TaskCard({ task }: { task: PlanTaskView }) {
  return (
    <div className={`task ${task.status}`}>
      <div className="task-head">
        <span className="task-id">{task.id}</span>
        <span className={`badge ${task.status}`}>{STATUS_LABEL[task.status]}</span>
      </div>
      <div className="task-desc">{task.description}</div>
      <div className="task-meta">依赖：{task.dependsOn.length > 0 ? task.dependsOn.join("、") : "无"}</div>
      {task.completionCriteria.length > 0 && (
        <ul className="task-criteria">
          {task.completionCriteria.map((criterion, i) => (
            <li key={i}>{criterion}</li>
          ))}
        </ul>
      )}
      {task.status === "failed" && task.error && <div className="task-error">⚠️ {task.error}</div>}
    </div>
  );
}

function Timeline({ steps }: { steps: TimelineStep[] }) {
  if (steps.length === 0) return null;
  return (
    <section className="plan-timeline">
      <h3>时间线</h3>
      <ol className="timeline">
        {steps.map((step) => (
          <li key={step.seq} className={`tl-row ${step.kind}`}>
            <span className="tl-seq">{step.seq}</span>
            <span className={`tl-lane ${step.taskId ? "task-lane" : "run-lane"}`}>{step.taskId || "运行"}</span>
            <span className="tl-icon">{STEP_ICON[step.kind]}</span>
            <span className="tl-text">{step.text}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function TaskPanel({
  open,
  onToggle,
  mode,
  plan,
  summary,
  replan,
  timeline,
}: {
  open: boolean;
  onToggle: () => void;
  mode: RunMode | null;
  plan: PlanTaskView[] | null;
  summary: RunSummary | null;
  replan: ReplanView | null;
  timeline: TimelineStep[];
}) {
  const done = plan?.filter((t) => t.status === "done").length ?? 0;
  // 直答模式没有计划是**正常的**，不是「还没开始规划」——空态文案必须说清楚，
  // 否则用户会一直等一个永远不会出现的计划。
  const emptyHint =
    mode === "react"
      ? "当前是直答模式，没有计划可展示；工具调用与每一步进展见下方时间线。"
      : "发一条消息，这里会实时显示 planner 拆出的任务。";

  return (
    <>
      {/* 收起后留一个细把手，别让面板「消失得找不回来」。 */}
      <button className={`panel-handle ${open ? "hidden" : ""}`} onClick={onToggle} title="展开任务编排">
        ‹
      </button>

      <aside className={`task-panel ${open ? "open" : ""}`} aria-hidden={!open}>
        <div className="panel-head">
          <h2>任务编排</h2>
          <div className="panel-head-right">
            {plan && (
              <span className="plan-progress">
                {done}/{plan.length}
              </span>
            )}
            <button className="panel-close" onClick={onToggle} title="收起">
              ›
            </button>
          </div>
        </div>

        {!plan && <div className="plan-empty">{emptyHint}</div>}

        <div className="task-list">
          {plan?.map((task) => (
            <TaskCard key={task.id} task={task} />
          ))}
        </div>

        <Timeline steps={timeline} />

        {replan && (
          <div className="plan-replan">
            复核：{replan.shouldContinue ? "继续" : "结束"}
            {replan.reason && <span className="replan-reason">（{replan.reason}）</span>}
          </div>
        )}

        {summary && (
          <div className="plan-foot">
            <div className={`summary-status ${summary.status}`}>
              {summary.status === "completed" ? "全部完成" : summary.status === "partial" ? "部分完成" : "已提前结束"}
            </div>
            {summary.failed.length > 0 && <div className="plan-foot-line">失败：{summary.failed.join("、")}</div>}
            {summary.skipped.length > 0 && <div className="plan-foot-line">跳过：{summary.skipped.join("、")}</div>}
          </div>
        )}
      </aside>
    </>
  );
}

// --------------------------------------------------------------------------- #
// 助手气泡：多轮过程折叠 + 最终回复
// --------------------------------------------------------------------------- #

/** 一轮的收起摘要：优先说调了什么，没有工具就说「思考」。 */
function summarizeRound(round: Round): string {
  const names = round.toolCalls.map((call) => call.name || "工具");
  return names.length === 0 ? "思考" : `调用 ${names.join("、")}`;
}

function RoundBlock({
  round,
  index,
  open,
  onToggle,
}: {
  round: Round;
  index: number;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <div className={`round ${open ? "open" : "closed"}`}>
      {/* 整行都是按钮：折叠的命中区域够大，键盘与读屏也用得上。 */}
      <button className="round-head" type="button" onClick={onToggle} aria-expanded={open}>
        <span className="round-caret">{open ? "▾" : "▸"}</span>
        <span className="round-label">第 {index + 1} 步</span>
        <span className="round-summary">{summarizeRound(round)}</span>
      </button>
      {open && (
        <div className="round-body">
          {round.text && <Markdown text={round.text} />}
          {/* 展开时把这一轮调了什么、带什么参数都摊开——收起时只留摘要行里的名字。 */}
          <ul className="round-tools">
            {round.toolCalls.map((call) => (
              <li key={call.index} className={`round-tool ${call.status}`}>
                <span className="round-tool-name">{call.name || "工具"}</span>
                <span className="round-tool-args">{call.args || "{}"}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

function ProcessBlock({ rounds, live }: { rounds: Round[]; live: boolean }) {
  // 折叠是纯 UI 偏好，所以放组件本地 state 而不是 reducer：reducer 是「事件 → 状态」的纯
  // 投影、实时与回放共用，把「我点开过哪一轮」塞进去会污染回放。键是轮序号——轮次只增
  // 不改，它在气泡的生命周期内稳定。
  const [overrides, setOverrides] = useState<Record<number, boolean>>({});
  const lastIndex = rounds.length - 1;

  return (
    <div className={`process ${live ? "live" : ""}`}>
      {rounds.map((round, index) => {
        // 默认规则：只有「正在打字的最后一轮」展开，历史轮全收起。用户点过就听用户的。
        const open = overrides[index] ?? (live && index === lastIndex);
        return (
          <RoundBlock
            key={index}
            round={round}
            index={index}
            open={open}
            onToggle={() => setOverrides((prev) => ({ ...prev, [index]: !open }))}
          />
        );
      })}
    </div>
  );
}

/**
 * 助手气泡 = 可折叠的多轮过程 + 最终回复。
 *
 * `live`（「这一轮还在打字」）在这里一次算清：**一旦最终回复有了字，过程就整体收起**——
 * 模型开始说终稿，过程就该让位，否则「边查边答」两截内容会同时铺开。
 */
function AssistantBubble({
  message,
  streaming,
  current,
}: {
  message: ChatMessage;
  streaming: boolean;
  current: boolean;
}) {
  const rounds = processRounds(message);
  const answer = finalText(message);

  return (
    <div className="bubble">
      {rounds.length > 0 && <ProcessBlock rounds={rounds} live={streaming && current && !answer} />}
      {answer && <Markdown text={answer} className="final" />}
      {message.note && <div className="note">⚠️ {message.note}</div>}
    </div>
  );
}

// --------------------------------------------------------------------------- #
// 页面
// --------------------------------------------------------------------------- #

export default function Home() {
  const {
    messages,
    mode,
    plan,
    summary,
    replan,
    timeline,
    threads,
    threadId,
    runId,
    streaming,
    error,
    send,
    cancel,
    openThread,
    startNewThread,
    rename,
    remove,
  } = useChat();
  const [input, setInput] = useState("");
  const [panelOpen, setPanelOpen] = useState(false);
  const autoShown = useRef(false);

  // 只在**首次**有数据时自动弹一次；之后完全由用户按钮控制，不再打扰。
  // 判据是「有没有数据」而不是「有没有计划」：直答模式永远不会有计划，只认 plan 的话
  // 面板在最主要的路径上从不自动出现。
  const hasData = Boolean(plan) || timeline.length > 0;
  useEffect(() => {
    if (!SHOW_TASK_PANEL) return; // 面板下线时整段短路，不留会 setState 的幽灵副作用
    if (hasData && !autoShown.current) {
      autoShown.current = true;
      setPanelOpen(true);
    }
  }, [hasData]);

  // 粘底滚动：折叠与实时打字会让内容在视口下方长出来。`stick` 默认贴底，用户往上翻就
  // 松手——自动滚动最招人烦的失败模式就是把人拽回去。
  const listRef = useRef<HTMLElement | null>(null);
  const stick = useRef(true);

  const onListScroll = () => {
    const el = listRef.current;
    if (!el) return;
    // 40px 的宽容度：否则惯性滚动的那一瞬就会被误判成「用户在上翻」。
    stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  };

  /** 滚轮落在右侧**任意位置**都要能滚对话。头部与输入框不是 `.messages` 的祖先，浏览器
   *  不会替我们转发；而指针已经在列表里时不能再插一手，否则会和浏览器自己的滚动叠加。 */
  const onChatWheel = (event: React.WheelEvent<HTMLElement>) => {
    const list = listRef.current;
    if (!list || list.contains(event.target as Node)) return;
    // deltaMode 1 = 按「行」计（部分鼠标/系统的默认），换算成像素，否则一格只滚几像素。
    list.scrollTop += event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
  };

  useEffect(() => {
    const el = listRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [messages, streaming]);

  /** 这个气泡属于**本轮**运行吗——回看历史时不该把旧气泡也判成「正在打字」。 */
  const isCurrentRun = (message: ChatMessage) =>
    Boolean(runId) && Boolean(message.key?.startsWith(`${runId}:`));

  const onSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!input.trim()) return;
    send(input);
    setInput("");
  };

  const onRename = (thread: ThreadSummary) => {
    const title = window.prompt("给这段对话起个名字", thread.title);
    if (title && title.trim()) void rename(thread.thread_id, title.trim());
  };

  const onRemove = (thread: ThreadSummary) => {
    // 真删不可逆（ADR-0009），所以必须确认。
    if (window.confirm(`删除「${thread.title}」？这段对话的记录会被永久删除。`)) {
      void remove(thread.thread_id);
    }
  };

  // 两处静默，各要一句占位：
  // - Plan-Exec：planner 用 structured output，不产 token 流，规划期一个字都没有；
  // - 主 Agent：还没有第一个正文 chunk 之前也是空的。
  // 判据只能看 mode——直答模式**永远**不会有 plan，用 `!plan` 当条件会让占位整轮挂着。
  const runMode = mode ?? "plan";
  // 判据必须是 `hasOutput` 而不是「有没有正文」：助手气泡不再有 content 字段（回复是从
  // 轮次推导的），沿用旧判据会**恒真**，占位符会整轮挂着。
  const awaitingFirstToken = streaming && !messages.some((m) => m.role === "assistant" && hasOutput(m));
  const placeholder = streaming && (runMode === "plan" ? !plan : awaitingFirstToken);
  const placeholderText = runMode === "react" ? "思考中…" : "规划中…";

  return (
    <div className="app">
      <SessionList
        threads={threads}
        currentId={threadId}
        busy={streaming}
        onOpen={(id) => void openThread(id)}
        onNew={startNewThread}
        onRename={onRename}
        onRemove={onRemove}
      />

      <main className="chat" onWheel={onChatWheel}>
        <header className="chat-header">
          <h1>EvaAI</h1>
          <span className="subtitle">企业级个人 AI 助手</span>
        </header>

        <section className="messages" ref={listRef} onScroll={onListScroll}>
          {messages.length === 0 && !placeholder && (
            <div className="empty">发一条消息开始对话，例如「现在几点？」</div>
          )}
          {messages.map((m, i) => (
            /* key 用气泡身份而不是数组下标：openThread 会整体替换 messages 数组，下标复用
               会把 A 对话的「第 2 轮展开」状态串到 B 对话上（折叠状态挂在组件本地）。 */
            <div key={m.key ?? `idx-${i}`} className={`msg ${m.role}`}>
              {/* 泳道后缀只对 Plan-Exec 有意义（`t1`/`t2`）。直答模式的泳道是约定的
                  `main`，标出来只会让人以为还有别的助手。 */}
              <div className="role">
                {m.role === "user" ? "你" : runMode === "plan" && m.taskId ? `EvaAI · ${m.taskId}` : "EvaAI"}
              </div>
              {m.role === "user" ? (
                <div className="bubble">{m.text}</div>
              ) : (
                (hasOutput(m) || streaming) && (
                  <AssistantBubble message={m} streaming={streaming} current={isCurrentRun(m)} />
                )
              )}
            </div>
          ))}
          {placeholder && (
            <div className="msg assistant">
              <div className="role">EvaAI</div>
              <div className="bubble">{placeholderText}</div>
            </div>
          )}
        </section>

        {error && <div className="error">⚠️ {error}</div>}

        <form className="input-row" onSubmit={onSubmit}>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="发消息给 EvaAI…"
            disabled={streaming}
          />
          {streaming ? (
            /* 运行中发送按钮换成「停止」：取消这一轮，终态（已取消）仍经 SSE 回来。 */
            <button type="button" onClick={cancel} title="停止本轮回答">
              停止
            </button>
          ) : (
            <button type="submit" disabled={!input.trim()}>
              发送
            </button>
          )}
        </form>
      </main>

      {SHOW_TASK_PANEL && (
        <TaskPanel
          open={panelOpen}
          onToggle={() => setPanelOpen((open) => !open)}
          mode={mode}
          plan={plan}
          summary={summary}
          replan={replan}
          timeline={timeline ?? EMPTY_STEP}
        />
      )}
    </div>
  );
}
