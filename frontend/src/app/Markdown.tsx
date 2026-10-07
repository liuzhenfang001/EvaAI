"use client";

import { memo, useDeferredValue } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

/**
 * 助手正文的 Markdown 渲染。
 *
 * 放在 `app/` 下与 `page.tsx` 同级：Next.js 的 App Router 只把 `page` / `layout` / `route`
 * 这类约定文件名当路由，其余文件就是普通模块。页面的 UI 组件都在 `page.tsx` 里，但这个
 * 单独成文件——它有一段自己的渲染策略要讲，混进那个已经五百行的文件里没人找得到。
 *
 * **为什么不用 DeerFlow 的 Streamdown**：那个库的样式靠 Tailwind 扫描它的产物生成
 * （DeerFlow 的 `globals.css` 用 `@source` 把 `node_modules/streamdown/dist` 号进扫描范围），
 * 而本项目前端是手写全局 CSS、**没有 Tailwind**——引入它等于顺带换掉整个样式方案。
 *
 * **为什么是「实时渲染」而不是「打完字再换 Markdown」**：后者在收尾那一刻会有一次整块重排，
 * 视觉跳变比半截 Markdown 的短暂错位更刺眼。半截围栏、写了一半的表格在 react-markdown 下
 * 会短暂显示成原样，随后自愈——可以接受。
 *
 * **不渲染裸 HTML**：react-markdown 默认如此，也刻意不装 `rehype-raw`——模型幻觉出来的
 * `<div>` 不该变成真实 DOM。
 */
function Markdown({ text, className }: { text: string; className?: string }) {
  // 流式期间每来一个 token 父组件就重渲染一次，而 ReactMarkdown 每次都要**从头**重新解析
  // 整段文本——长回复会因此掉帧。useDeferredValue 让解析落后于打字节奏：文字先按上一次的
  // 解析结果显示，解析完再补上。DeerFlow 的 markdown-content.tsx 用的是同一个手法。
  const deferred = useDeferredValue(text);

  return (
    <div className={className ? `md ${className}` : "md"}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          // 外链新开标签页：助手给的引用点开不该把当前对话顶掉。
          // `node` 是 react-markdown 塞进来的 AST 节点，透传给 DOM 会触发 React 未知属性警告。
          a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />,
        }}
      >
        {deferred}
      </ReactMarkdown>
    </div>
  );
}

// memo：折叠/展开某个轮次时父组件会重渲染，而正文一个字都没变——不该为此重新解析一遍
// Markdown。流式打字时 text 每次都变，这条 memo 不会命中，也不会挡着更新。
export default memo(Markdown);
