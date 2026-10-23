"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  CATEGORY_LABELS,
  MemoryFact,
  MemorySnapshot,
  MemoryStatus,
  SECTION_LABELS,
  clearMemory,
  createFact,
  deleteFact,
  formatTime,
  loadMemory,
  loadMemoryStatus,
  updateFact,
} from "@/core/memory";

/**
 * 记忆管理页。
 *
 * 这一页存在的理由是**用户得能自己抹掉模型记岔的东西**——长期记忆是模型自己写进库的，
 * 而「记岔了」是必然会发生的（把推测当事实、把一次性细节当长期偏好）。没有这一页，唯一的
 * 修法是拿 sqlite 客户端去改库。
 *
 * 交互上沿用聊天页那套最小做法：**不可逆的动作一律 `window.confirm`**（删一条、清空全部），
 * 编辑用 `window.prompt`（与重命名会话同一个取舍——一个本地单用户的工具，不值得为改一行字
 * 引入一整套表单状态）。
 */
export default function MemoryPage() {
  const [snapshot, setSnapshot] = useState<MemorySnapshot | null>(null);
  const [status, setStatus] = useState<MemoryStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [draft, setDraft] = useState("");
  const [draftCategory, setDraftCategory] = useState("context");

  const refresh = useCallback(async () => {
    try {
      const [nextStatus, nextSnapshot] = await Promise.all([loadMemoryStatus(), loadMemory()]);
      setStatus(nextStatus);
      setSnapshot(nextSnapshot);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "读取记忆失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  /** 每个动作都跑同一条骨架：做 → 重新拉一份 → 失败就显示。**重新拉而不是本地改 state**
   *  是有意的：排序、去重、时间戳都由后端定，本地猜一份就会出现「页面显示的和库里不一样」。 */
  const run = async (action: () => Promise<unknown>) => {
    try {
      await action();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
  };

  const onAdd = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!draft.trim()) return;
    void run(async () => {
      await createFact({ content: draft.trim(), category: draftCategory });
      setDraft("");
    });
  };

  const onEdit = (fact: MemoryFact) => {
    const content = window.prompt("改成什么？", fact.content);
    if (content && content.trim() && content.trim() !== fact.content) {
      void run(() => updateFact(fact.id, { content: content.trim() }));
    }
  };

  const onRemove = (fact: MemoryFact) => {
    if (window.confirm(`忘掉这条？\n\n${fact.content}`)) {
      void run(() => deleteFact(fact.id));
    }
  };

  const onClear = () => {
    if (window.confirm("清空全部长期记忆？画像与事实都会被永久删除，不可恢复。")) {
      void run(clearMemory);
    }
  };

  return (
    <div className="memory-page">
      <header className="memory-header">
        <Link href="/" className="memory-back">
          ← 回到对话
        </Link>
        <h1>长期记忆</h1>
        <span className="subtitle">
          {status && !status.enabled
            ? "这个部署没开长期记忆（config.yaml 里的 memory.enabled）"
            : `跨会话记住关于你的事：${status?.facts ?? 0} 条事实、${status?.profile_blocks ?? 0} 块画像`}
        </span>
        {snapshot && snapshot.facts.length > 0 && (
          <button className="memory-clear" onClick={onClear}>
            清空全部
          </button>
        )}
      </header>

      {error && <div className="error">⚠️ {error}</div>}
      {loading && <div className="memory-empty">读取中…</div>}

      {snapshot && snapshot.profile.length > 0 && (
        <section className="memory-section">
          <h2>画像</h2>
          <p className="memory-hint">由对话自动积累的概述，你不需要手动维护。</p>
          <ul className="profile-list">
            {snapshot.profile.map((block) => (
              <li key={block.section}>
                <span className="profile-label">{SECTION_LABELS[block.section] ?? block.section}</span>
                <span className="profile-summary">{block.summary}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {snapshot && (
        <section className="memory-section">
          <h2>事实</h2>
          <form className="memory-add" onSubmit={onAdd}>
            <input
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="记一条，例如「偏好用中文回答」"
            />
            <select
              value={draftCategory}
              onChange={(event) => setDraftCategory(event.target.value)}
              aria-label="分类"
            >
              {snapshot.categories.map((category) => (
                <option key={category} value={category}>
                  {CATEGORY_LABELS[category] ?? category}
                </option>
              ))}
            </select>
            <button type="submit" disabled={!draft.trim()}>
              记住
            </button>
          </form>

          {snapshot.facts.length === 0 ? (
            <div className="memory-empty">
              还没有记住任何事。对话里提到的事会被自动整理进来，你也可以直接在上面加一条。
            </div>
          ) : (
            <ul className="fact-list">
              {snapshot.facts.map((fact) => (
                <li key={fact.id} className="fact-item">
                  <div className="fact-body">
                    <span className="fact-content">{fact.content}</span>
                    <span className="fact-meta">
                      <span className={`fact-category ${fact.category}`}>
                        {CATEGORY_LABELS[fact.category] ?? fact.category}
                      </span>
                      <span>置信度 {fact.confidence.toFixed(2)}</span>
                      <span>{formatTime(fact.created_at)}</span>
                      {fact.source_thread && <span>来自会话 {fact.source_thread.slice(0, 8)}…</span>}
                    </span>
                  </div>
                  <span className="fact-actions">
                    <button title="修改" onClick={() => onEdit(fact)}>
                      ✎
                    </button>
                    <button title="忘掉" onClick={() => onRemove(fact)}>
                      🗑
                    </button>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
    </div>
  );
}
