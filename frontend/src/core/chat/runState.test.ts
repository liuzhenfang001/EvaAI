/** `runState.ts` 的契约测试。
 *
 * 为什么值得一个测试文件：`applyMessages` 是前端**唯一**一处「输入是一串外部事件、输出是
 * 结构化状态」的地方，它的 bug 全部长成「某个 chunk 序列下的状态错」——正是表驱动测试的
 * 靶心。而过去三轮前端 bug 都只能靠在 node 里手工单编发现。
 *
 * 下面几组用的 chunk 序列**逐字取自真实 journal**（`data/journal/4464fa70c7da`），
 * 不是构造出来的理想输入。
 */

import { describe, expect, it } from "vitest";
import {
  applyFrame,
  ChatMessage,
  emptyRunState,
  finalText,
  hasOutput,
  processRounds,
  Round,
  RunState,
  toFrame,
} from "./runState";

type Frame = [string, unknown];

/** 把一串帧依次喂给投影。 */
function project(frames: Frame[]): RunState {
  return frames.reduce((state, [name, data]) => applyFrame(state, name, data), emptyRunState());
}

/** 实时 `messages` 帧的线上形状：`data = [chunk, metadata]`（backend/app/gateway/sse.py）。 */
function model(payload: Record<string, unknown>): Frame {
  return ["messages", [{ type: "AIMessageChunk", ...payload }, { task_id: "main" }]];
}

function text(content: string): Frame {
  return model({ content });
}

function toolResult(name: string, callId: string, content = "结果"): Frame {
  return ["messages", [{ type: "tool", name, tool_call_id: callId, content }, { task_id: "main" }]];
}

/** 回放：journal 存的是扁平事件，必须先经 `toFrame` 还原成实时帧的形状。 */
function replay(events: Record<string, unknown>[]): RunState {
  return events.reduce<RunState>((state, event) => {
    const frame = toFrame(event as Parameters<typeof toFrame>[0]);
    return applyFrame(state, frame.event, frame.data);
  }, emptyRunState());
}

const START: Frame = ["run_start", { run_id: "r0", thread_id: "t0", request: "现在几点？", mode: "react" }];

function assistant(state: RunState): ChatMessage {
  const found = state.messages.filter((m) => m.role === "assistant");
  expect(found).toHaveLength(1);
  return found[0];
}

function calls(message: ChatMessage) {
  return message.rounds.flatMap((round) => round.toolCalls);
}

// --------------------------------------------------------------------------- #
// 工具调用：按 index 合并，名字增量不能覆盖
// --------------------------------------------------------------------------- #

describe("工具调用", () => {
  /** 真实序列：第一块两个视图都带名字，第二块只有参数是有效增量。 */
  const realJournal: Frame[] = [
    START,
    model({
      tool_calls: [{ name: "get_current_time", args: {}, id: "call_1", type: "tool_call" }],
      tool_call_chunks: [{ name: "get_current_time", args: "", id: "call_1", index: 0, type: "tool_call_chunk" }],
    }),
    model({
      tool_calls: [{ name: "", args: {}, id: null, type: "tool_call" }],
      tool_call_chunks: [{ name: null, args: "{}", id: null, index: 0, type: "tool_call_chunk" }],
    }),
    toolResult("get_current_time", "call_1", "2026-10-02T17:54:29"),
  ];

  it("一次调用只留一条，不是每个 chunk 追加一条", () => {
    expect(calls(assistant(project(realJournal)))).toHaveLength(1);
  });

  it("名字始终是 get_current_time——不是空白、也不是「工具」", () => {
    // 逐条断言而不是只看第一条：只看 [0] 的话，「追加」型的回归会漏过去（第一条永远是对的）。
    const names = calls(assistant(project(realJournal))).map((c) => c.name);
    expect(names).toEqual(["get_current_time"]);
    expect(names).not.toContain("");
    expect(names).not.toContain("工具");
  });

  it("参数片解析失败（tool_calls 被派生成空数组）时不回退成「工具」", () => {
    // 第二块的 `args` 是半截 JSON，langchain 的派生视图在这里是 []，只剩 chunks 可用。
    const state = project([
      START,
      model({ tool_call_chunks: [{ name: "ls", args: '{"path"', id: "call_9", index: 0 }] }),
      model({ tool_call_chunks: [{ name: null, args: ':"."}', id: null, index: 0 }] }),
    ]);
    expect(calls(assistant(state)).map((c) => c.name)).toEqual(["ls"]);
  });

  it("首块参数为空时，不从派生的 tool_calls 回填（否则会拼成 `{}{}`）", () => {
    // 真实流就是这两块：第一块 `args: ""`，第二块 `args: "{}"`。第一块的 `tool_calls`
    // 已经能从空增量派生出 `{}`，拿它回填再拼第二块就重复了。
    const state = project([
      START,
      model({
        tool_calls: [{ name: "get_current_time", args: {}, id: "call_1" }],
        tool_call_chunks: [{ name: "get_current_time", args: "", id: "call_1", index: 0 }],
      }),
      model({
        tool_calls: [{ name: "", args: {}, id: null }],
        tool_call_chunks: [{ name: null, args: "{}", id: null, index: 0 }],
      }),
    ]);
    expect(calls(assistant(state))[0].args).toBe("{}");
  });

  it("参数按增量拼成完整 JSON", () => {
    const state = project([
      START,
      model({ tool_call_chunks: [{ name: "ls", args: '{"path"', id: "c", index: 0 }] }),
      model({ tool_call_chunks: [{ name: null, args: ':"."}', id: null, index: 0 }] }),
    ]);
    expect(calls(assistant(state))[0].args).toBe('{"path":"."}');
  });

  it("参数到上限就封顶，不无限拼接", () => {
    // `write_file` 的参数里可能是整个文件——历史步骤不需要全文，状态和 DOM 也不该跟着涨。
    const piece = "y".repeat(5000);
    const state = project([
      START,
      model({ tool_call_chunks: [{ name: "write_file", args: piece, id: "c", index: 0 }] }),
      model({ tool_call_chunks: [{ name: null, args: piece, id: null, index: 0 }] }),
      model({ tool_call_chunks: [{ name: null, args: piece, id: null, index: 0 }] }),
    ]);
    const args = calls(assistant(state))[0].args;

    expect(args.length).toBeLessThan(piece.length); // 远小于「三块拼起来」
    expect(args.endsWith("…")).toBe(true);
  });

  it("结果按 tool_call_id 归属，乱序到达也各归各位", () => {
    const message = assistant(
      project([
        START,
        model({
          tool_call_chunks: [
            { name: "a", args: "{}", id: "ca", index: 0 },
            { name: "b", args: "{}", id: "cb", index: 1 },
          ],
        }),
        toolResult("b", "cb"),
        toolResult("a", "ca"),
      ]),
    );
    const found = calls(message);
    expect(found.map((c) => c.name)).toEqual(["a", "b"]); // 按 index 排序，不受到达序影响
    expect(found.map((c) => c.status)).toEqual(["done", "done"]);
  });
});

// --------------------------------------------------------------------------- #
// 轮次归并
// --------------------------------------------------------------------------- #

describe("轮次归并", () => {
  it("调用 → 结果 → 正文：两轮，后者是最终回复", () => {
    const message = assistant(
      project([
        START,
        model({ tool_call_chunks: [{ name: "get_current_time", args: "{}", id: "c1", index: 0 }] }),
        toolResult("get_current_time", "c1"),
        text("现在是 17:54。"),
      ]),
    );

    expect(message.rounds).toHaveLength(2);
    expect(processRounds(message)).toHaveLength(1);
    expect(finalText(message)).toBe("现在是 17:54。");
  });

  it("纯闲聊：没有过程轮，正文即最终回复", () => {
    const message = assistant(project([START, text("你好呀")]));

    expect(message.rounds).toHaveLength(1);
    expect(processRounds(message)).toHaveLength(0);
    expect(finalText(message)).toBe("你好呀");
  });

  it("多轮循环：每次「结果 → 正文 → 再调用」都开新轮", () => {
    const message = assistant(
      project([
        START,
        model({ tool_call_chunks: [{ name: "a", args: "{}", id: "c1", index: 0 }] }),
        toolResult("a", "c1"),
        text("再查一个"),
        model({ tool_call_chunks: [{ name: "b", args: "{}", id: "c2", index: 0 }] }),
        toolResult("b", "c2"),
        text("好了"),
      ]),
    );

    expect(message.rounds.map((r: Round) => r.text)).toEqual(["", "再查一个", "好了"]);
    expect(processRounds(message)).toHaveLength(2);
    expect(finalText(message)).toBe("好了");
  });

  it("一条结果落地就关闭本轮", () => {
    const message = assistant(
      project([
        START,
        model({ tool_call_chunks: [{ name: "a", args: "{}", id: "c1", index: 0 }] }),
        toolResult("a", "c1"),
      ]),
    );
    expect(message.rounds[0].closed).toBe(true);
  });

  it("结果先于调用到达：不新开一轮（结果不是模型的发言）", () => {
    const message = assistant(project([START, text("查一下"), toolResult("x", "cx")]));
    expect(message.rounds).toHaveLength(1);
  });

  it("调用没有结果（运行中断）：只有过程，没有最终回复", () => {
    const message = assistant(
      project([START, model({ tool_call_chunks: [{ name: "slow", args: "{}", id: "c1", index: 0 }] })]),
    );

    expect(calls(message)[0].status).toBe("running");
    expect(finalText(message)).toBe("");
    expect(hasOutput(message)).toBe(true); // 有过程就算有输出——占位符不该再挂着
  });
});

// --------------------------------------------------------------------------- #
// 这场改造的地基：实时与回放是同一个投影
// --------------------------------------------------------------------------- #

describe("实时与回放等价", () => {
  const events: Frame[] = [
    START,
    model({ tool_call_chunks: [{ name: "get_current_time", args: "{}", id: "c1", index: 0 }] }),
    toolResult("get_current_time", "c1"),
    text("现在是 17:54。"),
    ["run_end", { status: "completed", output: "现在是 17:54。" }],
  ];

  it("同一串事件走回放路径，messages 与实时逐字相同", () => {
    const journal = events.map(([name, data]) => {
      if (name !== "messages") return { type: name, ...(data as object) };
      const [chunk, metadata] = data as [Record<string, unknown>, Record<string, unknown>];
      return { type: "messages", chunk, task_id: metadata.task_id, metadata: {} };
    });

    expect(replay(journal).messages).toEqual(project(events).messages);
  });

  it("最终回复来自轮次推导——不依赖 run_end 的 output", () => {
    // 封存的 Plan-Exec 会话的 journal 里根本没有 run_end（实测 12 个），
    // 靠它判定会让那批历史整段渲染成「全是过程、没有回答」。
    const message = assistant(
      project([
        START,
        model({ tool_call_chunks: [{ name: "a", args: "{}", id: "c1", index: 0 }] }),
        toolResult("a", "c1"),
        text("答案在过程之后"),
      ]),
    );
    expect(finalText(message)).toBe("答案在过程之后");
  });
});

// --------------------------------------------------------------------------- #
// 失败备注
// --------------------------------------------------------------------------- #

describe("失败备注", () => {
  it("运行失败写进 note，最终回复保持完整", () => {
    // 旧实现把备注拼进正文，会把一段完整的回答截成半句。
    const message = assistant(project([START, text("答案"), ["run_end", { status: "failed", error: "boom" }]]));

    expect(finalText(message)).toBe("答案");
    expect(message.note).toContain("boom");
  });

  it("任务失败也写进 note", () => {
    const message = assistant(
      project([START, ["task_start", { task_id: "t1" }], ["task_end", { task_id: "t1", status: "failed", error: "炸了" }]]),
    );

    expect(message.note).toContain("炸了");
  });
});

// --------------------------------------------------------------------------- #
// 取消备注（T5）
// --------------------------------------------------------------------------- #

describe("取消备注", () => {
  it("运行取消写进 note，且不算失败", () => {
    // 取消是独立的终态，不是失败——文案不该带「失败」二字（ADR-0005）。
    const message = assistant(
      project([
        START,
        model({ tool_call_chunks: [{ name: "slow", args: "{}", id: "c1", index: 0 }] }),
        ["run_end", { status: "cancelled" }],
      ]),
    );

    expect(message.note).toBe("已取消");
    expect(message.note).not.toContain("失败");
  });

  it("取消时最终回复保持完整，备注不拼进正文", () => {
    // 与失败同一规矩：备注进独立的 note，不截断「模型已经说了什么」。
    const message = assistant(
      project([START, text("查到了，是 18:04"), ["run_end", { status: "cancelled" }]]),
    );

    expect(finalText(message)).toBe("查到了，是 18:04");
    expect(message.note).toBe("已取消");
  });
});

// --------------------------------------------------------------------------- #
// 思考内容（T12）
// --------------------------------------------------------------------------- #

describe("思考内容", () => {
  /** 带思考的模型消息：后端 `DeepSeekChatModel` 把 `reasoning_content` 捞进了 additional_kwargs。 */
  function thought(text: string): Frame {
    return model({ additional_kwargs: { reasoning_content: text } });
  }

  it("思考自成一步，正文另一步", () => {
    // 合成一步再在内部拆的话，界面上的「第 N 步」会与实际发生的事对不上。
    const message = assistant(project([START, thought("先看看"), text("现在是 18:04")]));

    expect(message.rounds.map((r) => [r.kind, r.text])).toEqual([
      ["thinking", "先看看"],
      ["answer", "现在是 18:04"],
    ]);
  });

  it("同一段思考的增量累加在同一步里", () => {
    const message = assistant(project([START, thought("我需要"), thought("先查工具")]));

    expect(message.rounds).toHaveLength(1);
    expect(message.rounds[0].text).toBe("我需要先查工具");
  });

  it("相邻两轮的思考各占一步，不接成一段", () => {
    // 中间隔着一次工具往返：第二段思考是新的一步，不是第一段的续写。
    const message = assistant(
      project([
        START,
        thought("先查"),
        model({ tool_call_chunks: [{ name: "get_time", args: "{}", id: "c1", index: 0 }] }),
        toolResult("get_time", "c1"),
        thought("拿到了"),
        text("18:04"),
      ]),
    );

    expect(message.rounds.map((r) => r.kind)).toEqual(["thinking", "answer", "thinking", "answer"]);
    expect(message.rounds[0].text).toBe("先查");
    expect(message.rounds[2].text).toBe("拿到了");
  });

  it("工具调用归到「说」那一步，不混进思考步", () => {
    const message = assistant(
      project([
        START,
        thought("该查时间"),
        model({ tool_call_chunks: [{ name: "get_time", args: "{}", id: "c1", index: 0 }] }),
      ]),
    );

    expect(message.rounds[1].kind).toBe("answer");
    expect(message.rounds[1].toolCalls).toHaveLength(1);
    expect(message.rounds[0].toolCalls).toHaveLength(0);
  });

  it("极速档下不产生思考步", () => {
    const message = assistant(project([START, text("直接答")]));

    expect(message.rounds.map((r) => r.kind)).toEqual(["answer"]);
  });

  it("只想了没说时，那段思考**不是**最终回复", () => {
    // 截断 / 超时 / 取消都会留下「有思考、没正文」的一轮。finalText 只看「最后一步没有工具调用」
    // 的话，会把模型的内心独白当成对用户的答复显示出来——用户会看到一段莫名其妙的自言自语。
    const message = assistant(project([START, thought("我应该回答 42，可是")]));

    expect(finalText(message)).toBe("");
  });

  it("思考步算「过程」，与最终回复分得开", () => {
    const message = assistant(project([START, thought("想想"), text("42")]));

    expect(finalText(message)).toBe("42");
    expect(processRounds(message).map((r) => r.kind)).toEqual(["thinking"]);
  });

  it("回放出来的思考与实时一致", () => {
    const live = assistant(project([START, thought("嗯"), text("答")]));
    const replayed = assistant(
      replay([
        { type: "run_start", run_id: "r0", thread_id: "t0", request: "q", mode: "react" },
        { type: "messages", task_id: "main", chunk: { type: "AIMessageChunk", additional_kwargs: { reasoning_content: "嗯" } } },
        { type: "messages", task_id: "main", chunk: { type: "AIMessageChunk", content: "答" } },
      ]),
    );

    expect(replayed.rounds).toEqual(live.rounds);
  });
});

describe("思考档位", () => {
  it("从 run_start 读出来", () => {
    const state = project([["run_start", { run_id: "r0", thread_id: "t0", request: "q", thinking: "max" }]]);

    expect(state.thinking).toBe("max");
  });

  it("老记录缺这个字段时回退成 off，不是请求层的默认档", () => {
    // 关键：缺字段的是 T12 之前的运行，那些运行**确实没有思考**。跟着请求层的默认（high）
    // 走会让旧记录凭空多出一段并不存在的思考——而回放的全部意义就是「与实时逐字一致」。
    const state = project([START]);

    expect(state.thinking).toBe("off");
  });

  it("空白状态就是 off", () => {
    expect(emptyRunState().thinking).toBe("off");
  });
});

// --------------------------------------------------------------------------- #
// 交付文件（T11）
// --------------------------------------------------------------------------- #

describe("交付文件", () => {
  const FILES = [
    { path: "/output/quicksort.go", name: "quicksort.go", size: 2048, mime: null },
    { path: "/output/报告.md", name: "报告.md", size: 512, mime: "text/markdown" },
  ];
  const PRESENTED: Frame = ["files_presented", { type: "files_presented", task_id: "main", files: FILES }];

  it("挂到对应的助手气泡上，字段原样保留", () => {
    // 这份文件列表的形状就是后端 FilesPresentedEvent 的在线形状（含 task_id 泳道）。
    const message = assistant(project([START, text("写好了"), PRESENTED]));

    expect(message.files).toEqual(FILES);
  });

  it("两次交付是累加，不是后一次覆盖前一次", () => {
    // 模型可以先交一份、再补一份；覆盖的话第一份卡片会凭空消失。
    const more = [
      { path: "/output/chart.png", name: "chart.png", size: 9000, mime: "image/png" },
    ];
    const message = assistant(
      project([
        START,
        text("先交这些"),
        PRESENTED,
        ["files_presented", { type: "files_presented", task_id: "main", files: more }],
      ]),
    );

    expect(message.files?.map((f) => f.path)).toEqual([
      "/output/quicksort.go",
      "/output/报告.md",
      "/output/chart.png",
    ]);
  });

  it("回放出来的与实时看到的完全一致", () => {
    // `toFrame` 对未知事件是原样透传，所以回放**不需要**为这个事件另写一条路径。
    // 这条就是钉住那个「不需要」。
    const live = assistant(project([START, text("写好了"), PRESENTED]));
    const replayed = assistant(
      replay([
        { type: "run_start", run_id: "r0", thread_id: "t0", request: "现在几点？", mode: "react" },
        { type: "files_presented", task_id: "main", files: FILES },
      ]),
    );

    expect(replayed.files).toEqual(live.files);
  });

  it("没有路径的条目被丢掉——卡片点不动就没意义", () => {
    const message = assistant(
      project([
        START,
        text("写好了"),
        ["files_presented", { type: "files_presented", task_id: "main", files: [{ name: "无路径.md" }, ...FILES] }],
      ]),
    );

    expect(message.files?.map((f) => f.path)).toEqual(["/output/quicksort.go", "/output/报告.md"]);
  });

  it("空列表不改变状态：不造空气泡、也不落一个空数组", () => {
    const before = project([START, text("写好了")]);
    const after = project([START, text("写好了"), ["files_presented", { type: "files_presented", task_id: "main", files: [] }]]);

    expect(after).toEqual(before);
  });

  it("只有文件也算「有内容」", () => {
    // hasOutput 是空气泡守卫：有卡片却判成空，气泡会被整块丢掉。
    const message = assistant(project([START, PRESENTED]));

    expect(message.rounds).toHaveLength(0);
    expect(hasOutput(message)).toBe(true);
  });
});
