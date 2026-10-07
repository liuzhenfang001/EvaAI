# 助手回复渲染为 Markdown

模型的回复一直是 Markdown（列表、代码块、粗体是它默认的组织方式），但前端把它当**纯文本**
渲染——`page.tsx` 里的 `{answer}` 直接插进 JSX，只靠 `white-space: pre-wrap` 保留换行。于是
`**粗体**` 和 ` ```go ` 都是原样显示的字面字符。本片把这条链补上：正文交给一个 Markdown
渲染组件，并把输出格式写进提示词成为**明确约定**而不是默认行为。

## 决策

**1. 用 `react-markdown` + `remark-gfm` + `rehype-highlight`，不用参照项目的 Streamdown。**

DeerFlow 用的是 Streamdown 2.5。**不能照搬**的原因是样式方案：Streamdown 的样式是 Tailwind
工具类，要靠使用方在自己的 CSS 里 `@source` 把它号进扫描范围（DeerFlow 的 `globals.css`
正是这么做的），而本项目前端是**手写全局 CSS、没有 Tailwind**（整个 `frontend/src` 只有
9 个源文件、没有 `components/` 目录）。引入 Streamdown 等于顺带换掉整个样式方案——为一个
渲染组件付这个代价不成比例。

`react-markdown` 是同一件事的轻量做法：它只管「Markdown → React 元素」，样式完全交给使用方。
配套两个插件：`remark-gfm` 补表格/删除线/任务列表/自动链接（CommonMark 之外、但模型天天在用的
那部分语法），`rehype-highlight` 给代码块上色。渲染组件在
`frontend/src/app/Markdown.tsx`，`.md` 子树的排版与代码高亮的配色写在 `globals.css` 里。

**2. 流式期间实时渲染，不做「打完字再换成 Markdown」。**

文本是逐 token 增长的，两种做法各有代价：实时渲染会让半截 Markdown（未闭合的围栏、写了一半的
表格）短暂显示成原样，随后自愈；而「先纯文本、收尾再换」会在最后一刻整块重排，跳变比前者更
刺眼。选前者，用 `useDeferredValue` 让解析落后于打字节奏——否则每来一个 token 都要从头重新
解析整段文本，长回复会掉帧（DeerFlow 的 `markdown-content.tsx` 是同一个手法）。

**3. 不渲染裸 HTML。**

react-markdown 默认不解析 HTML，本片也**刻意不装** `rehype-raw`：模型幻觉出来的 `<div>` 不该
变成真实 DOM。参照项目的消息渲染同样默认用它禁掉 raw HTML 的那个插件变体。

**4. 代码高亮的配色手写，不引入 highlight.js 的主题 css。**

本项目没有设计 token，配色都是就地写在 `globals.css` 里的。跟着 npm 走一份上游主题，只会多出
一处需要人工对齐的东西（而且要覆盖它的配色时，写的规则比直接写一遍还长）。色值取 github-dark
的暗色系，注释灰改用本页既有的 `#8a8f98`。

**5. 输出格式写进提示词。**

`app/prompts.py` 的 `SYSTEM_PROMPT` 原先没提输出格式。补一句明确约定，口径取参照项目
`<response_style>` 的**克制**那一面：可以用 Markdown，但别过度格式化——一段话说得清的事不要
拆成小标题加一堆列表。不这么写的话，模型很容易把三句话的答案排成三层标题。

## 后果

- **前端多三处依赖**：`react-markdown` / `remark-gfm` / `rehype-highlight`。首屏 JS 从
  ~104 kB 涨到 ~201 kB——大头是 `rehype-highlight` 默认带的 `common` 语言集（约 37 种）。
  要瘦身就给它传一个语言白名单（`rehypeHighlight` 的 `languages` 选项），代价是冷门语言不上色；
  本片按「本地个人助手，宽覆盖更划算」保留默认。
- **过程轮次正文与最终回复走同一个组件**：两者是同一个渲染器的不同容器，只做一半会让
  「过程」里的代码块还是字面字符。
- `.md` 子树里 `white-space` 必须显式设回 `normal`：气泡祖先为纯文本设了 `pre-wrap`，
  而 white-space 会继承——忘了这条，Markdown 生成的每个块之间会多出一行空行。
- **本片没有自动化测试**。渲染是 UI 行为，验它需要 jsdom + `@testing-library/react`（现在都没装），
  而前端既有的测试基建（`runState.test.ts`）测的是纯 reducer。不夹带引入组件测试基建——
  那是一次独立的投入，该单独开一片。验证走端到端目视 + `npm run build` 的类型检查。

## 不做 / 范围外

- **Mermaid、KaTeX**：参照项目都接了，但那是「画图与公式」两类专门需求，与本片「把已有语法
  渲染正确」不是一件事。
- **流式对齐的 polish**（DeerFlow 有「隐藏尾部空 `<li>` 防止孤立列表项」这类处理）：本片不预设，
  实测抖动明显再补。
- **文件产出的展示**：与本片同批提出，但是独立一片（`present_files` 工具 + 下载路由），
  见 [issue #11](https://github.com/liuzhenfang001/EvaAI/issues/11)——它的设计已在 issue 里定型，
  实现时另出一份 ADR。
