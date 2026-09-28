"use client";

import { useState } from "react";
import { PlanTaskView, ReplanView, RunSummary, TaskStatus, useChat } from "@/core/chat/useChat";

const STATUS_LABEL: Record<TaskStatus, string> = {
  pending: "待执行",
  running: "执行中",
  done: "已完成",
  failed: "失败",
  skipped: "已跳过",
};

function PlanPanel({
  plan,
  summary,
  replan,
}: {
  plan: PlanTaskView[] | null;
  summary: RunSummary | null;
  replan: ReplanView | null;
}) {
  const done = plan?.filter((t) => t.status === "done").length ?? 0;

  return (
    <aside className="plan-panel">
      <div className="plan-head">
        <h2>计划</h2>
        {plan && (
          <span className="plan-progress">
            {done}/{plan.length}
          </span>
        )}
      </div>

      {!plan && <div className="plan-empty">发送一条消息，这里会显示 planner 拆出的任务。</div>}

      <div className="task-list">
        {plan?.map((task) => (
          <div key={task.id} className={`task ${task.status}`}>
            <div className="task-head">
              <span className="task-id">{task.id}</span>
              <span className={`badge ${task.status}`}>{STATUS_LABEL[task.status]}</span>
            </div>
            <div className="task-desc">{task.description}</div>
            <div className="task-meta">
              依赖：{task.dependsOn.length > 0 ? task.dependsOn.join("、") : "无"}
            </div>
            {task.completionCriteria.length > 0 && (
              <ul className="task-criteria">
                {task.completionCriteria.map((criterion, i) => (
                  <li key={i}>{criterion}</li>
                ))}
              </ul>
            )}
            {task.status === "failed" && task.error && <div className="task-error">⚠️ {task.error}</div>}
          </div>
        ))}
      </div>

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
  );
}

export default function Home() {
  const { messages, streaming, error, plan, summary, replan, send } = useChat();
  const [input, setInput] = useState("");

  const onSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!input.trim()) return;
    send(input);
    setInput("");
  };

  return (
    <div className="app">
      <main className="chat">
        <header className="chat-header">
          <h1>EvaAI</h1>
          <span className="subtitle">企业级个人 AI 助手</span>
        </header>

        <section className="messages">
          {messages.length === 0 && (
            <div className="empty">发一条消息开始对话，例如「现在几点？」</div>
          )}
          {messages.map((m, i) => (
            <div key={i} className={`msg ${m.role}`}>
              <div className="role">{m.role === "user" ? "你" : "EvaAI"}</div>
              <div className="bubble">
                {m.content || (m.role === "assistant" && streaming ? (plan ? "执行中…" : "规划中…") : "")}
              </div>
              {m.tools.length > 0 && (
                <div className="tools">
                  {m.tools.map((t, j) => (
                    <span key={j} className="tool">🔧 {t.label}</span>
                  ))}
                </div>
              )}
            </div>
          ))}
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

      <PlanPanel plan={plan} summary={summary} replan={replan} />
    </div>
  );
}
