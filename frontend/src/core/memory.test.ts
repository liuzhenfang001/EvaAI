/** `memory.ts` 里纯函数与那两张标签表的契约测试。
 *
 * 标签表判错的后果是**静默**的：后端新加一个分类（或者改了个 section 名），下拉框里就会
 * 冒出一个英文原样、或者干脆漏掉一项——页面不报错，只是看着像没做完。所以这里把「前端认的
 * 清单」与「后端确实会发的取值」钉在一起。
 *
 * 后端的取值来源：分类 = `evaharness/memory/schema.py:FACT_CATEGORIES`（页面靠
 * `/memory` 响应里的 `categories` 渲染，这里是兜底的中文名），画像块 =
 * `ProfileSection` 的六个值。
 */

import { describe, expect, it } from "vitest";
import { CATEGORY_LABELS, SECTION_LABELS, formatTime } from "./memory";

const BACKEND_CATEGORIES = [
  "preference",
  "correction",
  "identity",
  "goal",
  "constraint",
  "decision",
  "behavior",
  "context",
  "other",
];

const BACKEND_SECTIONS = [
  "work_context",
  "personal_context",
  "top_of_mind",
  "recent_months",
  "earlier_context",
  "long_term_background",
];

describe("标签表", () => {
  it("覆盖后端会发的全部分类", () => {
    for (const category of BACKEND_CATEGORIES) {
      expect(CATEGORY_LABELS[category], `缺 ${category} 的中文名`).toBeTruthy();
    }
    expect(Object.keys(CATEGORY_LABELS).sort()).toEqual([...BACKEND_CATEGORIES].sort());
  });

  it("覆盖六块画像", () => {
    for (const section of BACKEND_SECTIONS) {
      expect(SECTION_LABELS[section], `缺 ${section} 的中文名`).toBeTruthy();
    }
    expect(Object.keys(SECTION_LABELS).sort()).toEqual([...BACKEND_SECTIONS].sort());
  });
});

describe("formatTime", () => {
  it("把后端给的 ISO 8601 渲染成本地时间", () => {
    expect(formatTime("2026-10-13T09:12:33+00:00")).not.toBe("—");
  });

  it("认不出来的时间戳显示成破折号，不是 NaN", () => {
    // 参照项目在这里栽过：后端发了个前端解析不了的时间戳，页面上出现一片
    // `Invalid time value`。宁可显示成一个明确的「没有」。
    expect(formatTime("")).toBe("—");
    expect(formatTime("not-a-date")).toBe("—");
  });
});
