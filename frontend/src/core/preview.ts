/** 文件预览的类型分派。
 *
 * 抽成纯函数是因为它**只有一个正确答案**、而且判错了只会静默地难看（一个 `.go` 被判成
 * 「不支持预览」，用户看到的就是一句没头没脑的话）。这类东西正是单测的靶心——前端目前
 * 只有 vitest + 纯函数这一种测试，所以逻辑要主动往这一层放。
 *
 * **判据以后缀为主、MIME 为辅**：后端的 `mimetypes.guess_type` 认不出大量代码后缀
 * （实测 `.go` 拿到的是 `null`），所以只看 MIME 会把代码文件全判成「不支持」。
 */

export type PreviewKind = "markdown" | "code" | "image" | "none";

const IMAGE_SUFFIXES = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif"]);
/** 可以**直接喂给 `<img>`** 的图片格式。
 *
 * **`svg` 刻意不在内**：它能带脚本。虽然在 `<img>` 里嵌 SVG 是安全的（图片上下文不执行
 * 脚本），但为它单开一条通道不划算；而且我们自己的下载路由已经把 svg 归进「强制附件」的
 * active content——两处口径一致比省一个后缀重要。
 */

const MARKDOWN_SUFFIXES = new Set(["md", "markdown"]);

const CODE_SUFFIXES = new Set([
  "txt", "log", "csv", "tsv", "json", "jsonl", "yaml", "yml", "toml", "ini", "cfg", "conf", "env",
  "go", "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "java", "kt", "swift", "rs", "rb", "php",
  "c", "h", "cc", "cpp", "hpp", "cs", "sh", "bash", "zsh", "ps1", "sql", "css", "scss", "xml",
]);
/** 当文本来看的后缀。判断不了的就落到 `none`——「不认识」和「是文本」不该混为一谈。 */

const TEXT_MIME_TYPES = new Set([
  "application/json",
  "application/xml",
  "application/javascript",
  "application/x-yaml",
  "application/sql",
]);
/** MIME 里不以 `text/` 开头、但确实是文本的那几个。 */

/** 后缀 → hljs 语言标识；不在表里就返回 `null`。
 *
 * 只映射到 highlight.js 的 **common** 语言集里存在的名字——`rehype-highlight` 默认就用那一套，
 * 给一个它不认识的标识轻则不上色、重则报错。所以 `tsx` / `jsx` 都落到 `typescript` /
 * `javascript`，而不是照抄后缀。
 */
const HLJS_LANGUAGES: Record<string, string> = {
  go: "go",
  py: "python",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "javascript",
  ts: "typescript",
  tsx: "typescript",
  java: "java",
  kt: "kotlin",
  swift: "swift",
  rs: "rust",
  rb: "ruby",
  php: "php",
  c: "c",
  h: "c",
  cc: "cpp",
  cpp: "cpp",
  hpp: "cpp",
  cs: "csharp",
  sh: "bash",
  bash: "bash",
  zsh: "bash",
  sql: "sql",
  css: "css",
  scss: "scss",
  xml: "xml",
  json: "json",
  jsonl: "json",
  yaml: "yaml",
  yml: "yaml",
  toml: "ini",
  ini: "ini",
  md: "markdown",
  markdown: "markdown",
};

/** 文件名的小写扩展名（不含点）；没有扩展名时返回空串。 */
export function suffixOf(name: string): string {
  const dot = name.lastIndexOf(".");
  // `dot <= 0`：`.gitignore` 这种点开头的是**整个名字**，不是「扩展名 gitignore」。
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

export function previewKind(name: string, mime: string | null): PreviewKind {
  const suffix = suffixOf(name);
  if (IMAGE_SUFFIXES.has(suffix)) {
    return "image";
  }
  if (MARKDOWN_SUFFIXES.has(suffix)) {
    return "markdown";
  }
  if (CODE_SUFFIXES.has(suffix) || TEXT_MIME_TYPES.has(mime ?? "") || (mime ?? "").startsWith("text/")) {
    return "code";
  }
  return "none";
}

/** 这个文件在预览里该用哪个语言上色；认不出来返回 `null`（围栏就不写语言）。 */
export function hljsLanguage(name: string): string | null {
  return HLJS_LANGUAGES[suffixOf(name)] ?? null;
}

/** 类型标签：`.md` → `MD`。没有扩展名就退化成「文件」，不硬猜。 */
export function typeLabel(name: string): string {
  const suffix = suffixOf(name);
  return suffix ? suffix.toUpperCase() : "文件";
}

/** 大小只给一个量级：卡片和预览的头部写精确字节数没人看，一位小数足够分辨 KB / MB。 */
export function formatSize(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** 服务端返回的是不是**被截断过**的内容。
 *
 * **判据不能是状态码。** 实测：`FileResponse` 对**任何**合法的 Range 请求都回 206——哪怕
 * 文件比请求的范围还小（100 字节的文件 + `bytes=0-262143` 照样是 206，而且给的是全文）。
 * 拿 206 当「截断了」会让每个小文件都顶着一句「文件较大，只显示了前 256 KB」。
 *
 * 真正的信号在 `Content-Range` 的**斜杠后面**：`bytes 0-99/100` 里的 `100` 才是文件总大小。
 * 服务端根本不理 Range（回 200、没有这个头）时，退化成用收到的字节数自己判。
 */
export function isTruncated(contentRange: string | null, receivedBytes: number, limit: number): boolean {
  const total = contentRange ? Number(contentRange.split("/")[1]) : Number.NaN;
  return Number.isFinite(total) ? total > receivedBytes : receivedBytes > limit;
}

/** 给一段文本配一个不会被它自己撑破的围栏。
 *
 * 预览代码走的是「把文本包成围栏代码块、交给 Markdown 组件」——高亮与样式于是全部复用，
 * 不必为预览再写一套。唯一要小心的是：文件内容里本来就可能出现反引号（Markdown 文件、
 * 文档里的示例），围栏必须比内容里**最长的一段**还长，否则结构会被提前闭合。
 */
export function fenceFor(text: string): string {
  let longest = 0;
  for (const run of text.match(/`+/g) ?? []) {
    longest = Math.max(longest, run.length);
  }
  return "`".repeat(Math.max(3, longest + 1));
}
