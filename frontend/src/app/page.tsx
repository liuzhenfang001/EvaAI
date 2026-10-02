"use client";

import { useEffect, useRef, useState } from "react";
import {
  PlanTaskView,
  ReplanView,
  RunMode,
  RunSummary,
  TaskStatus,
  ThreadSummary,
  TimelineKind,
  TimelineStep,
  useChat,
} from "@/core/chat/useChat";

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
    streaming,
    error,
    send,
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
    if (hasData && !autoShown.current) {
      autoShown.current = true;
      setPanelOpen(true);
    }
  }, [hasData]);

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
  const awaitingFirstToken = streaming && !messages.some((m) => m.role === "assistant" && m.content);
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

      <main className="chat">
        <header className="chat-header">
          <h1>EvaAI</h1>
          <span className="subtitle">企业级个人 AI 助手</span>
        </header>

        <section className="messages">
          {messages.length === 0 && !placeholder && (
            <div className="empty">发一条消息开始对话，例如「现在几点？」</div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              {/* 泳道后缀只对 Plan-Exec 有意义（`t1`/`t2`）。直答模式的泳道是约定的
                  `main`，标出来只会让人以为还有别的助手。 */}
              <div className="role">
                {m.role === "user" ? "你" : runMode === "plan" && m.taskId ? `EvaAI · ${m.taskId}` : "EvaAI"}
              </div>
              {(m.content || m.tools.length > 0) && (
                <div className="bubble">
                  {m.content}
                  {m.tools.length > 0 && (
                    <div className="tools">
                      {m.tools.map((t, j) => (
                        <span key={j} className="tool">
                          🔧 {t.label}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
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
          <button type="submit" disabled={streaming || !input.trim()}>
            {streaming ? "…" : "发送"}
          </button>
        </form>
      </main>

      <TaskPanel
        open={panelOpen}
        onToggle={() => setPanelOpen((open) => !open)}
        mode={mode}
        plan={plan}
        summary={summary}
        replan={replan}
        timeline={timeline ?? EMPTY_STEP}
      />
    </div>
  );
}
