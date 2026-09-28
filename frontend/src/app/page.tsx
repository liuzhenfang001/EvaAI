"use client";

import { useState } from "react";
import { useChat } from "@/core/chat/useChat";

export default function Home() {
  const { messages, streaming, error, send } = useChat();
  const [input, setInput] = useState("");

  const onSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!input.trim()) return;
    send(input);
    setInput("");
  };

  return (
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
              {m.content || (m.role === "assistant" && streaming ? "思考中…" : "")}
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
  );
}
