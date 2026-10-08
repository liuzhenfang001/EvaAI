/** 用户偏好：那些「记住我上次选了什么」的前端设置。
 *
 * 与 `session.ts` 里那对 `readCurrentThreadId` / `rememberCurrentThreadId` 是同一个模式
 * （localStorage + 一读一写），但**关心点不同**：那个是「现在在哪段对话」，这个是「我习惯
 * 怎么用」。后者只会越来越多（模型档、面板开合…），所以单独一个模块放，别往会话那个里堆。
 *
 * **为什么不放进 reducer**：偏好不是「事件投影出来的状态」——它是用户的选择，不随运行变化。
 * 塞进 reducer 会污染回放：同一条 journal 在两个人机器上会渲染出不同的东西，而回放的
 * 全部意义就是「与实时逐字一致」。
 */

import { ThinkingLevel } from "@/core/chat/runState";

const THINKING_KEY = "evaai.thinking";

/** 面向用户的默认档：**开思考**。与后端 `ChatRequest.thinking` 的缺省保持一致。 */
export const DEFAULT_THINKING: ThinkingLevel = "high";

const LEVELS: ThinkingLevel[] = ["off", "low", "high", "max"];

export function readThinkingLevel(): ThinkingLevel {
  if (typeof window === "undefined") {
    return DEFAULT_THINKING;
  }
  const raw = window.localStorage.getItem(THINKING_KEY);
  // 只认白名单。localStorage 里的值可能来自旧版本（或者被人手改过），直接信任它会让一个
  // 后端不认的档位一路发出去换回一个 422——而错误会出现在「发消息」那一刻，很难联想到
  // 「上次存的档位」。
  return LEVELS.includes(raw as ThinkingLevel) ? (raw as ThinkingLevel) : DEFAULT_THINKING;
}

export function rememberThinkingLevel(level: ThinkingLevel): void {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(THINKING_KEY, level);
}
