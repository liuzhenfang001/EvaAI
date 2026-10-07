/** 前端访问 gateway 的唯一地址来源。
 *
 * 刻意只留一处：这个默认值原先在 useChat/session 里各写了一遍，改端口要改两个文件
 * 且容易漏。要用别的地址（例如后端换了端口）就在 `frontend/.env.local` 里写
 * `NEXT_PUBLIC_API_URL=...`——Next.js 只从**前端目录**读环境文件，仓库根的 `.env`
 * 是给后端用的，那边设了不起作用。
 *
 * 端口取 8010 而不是常见的 8000/8001：本机同时跑着参照项目 DeerFlow，它占的就是
 * 8001（后端）与 3000（前端）。
 */

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8010";

/**
 * 交付文件的地址（T11）。
 *
 * 事件给的是**虚拟路径**（`/output/报告.md`），而后端路由的形状是
 * `/threads/{会话}/files/{相对路径}`——`/output` 前缀在这里摘掉。这个映射是与
 * `backend/app/gateway/routers/files.py` 成对的，改一处就得改另一处，所以写在地址
 * 这一处而不是散到各个调用方去 `replace`。
 *
 * 逐段编码而不是整体 `encodeURIComponent`：路径里允许带 `/`（交付物可以放在子目录），
 * 整体编码会把分隔符也吃掉。
 *
 * ``download`` 默认 **false**：同一个地址，预览拿它 inline 取内容、下载按钮拿它要附件
 * （见 `FilePreview` 与 `FileCards`）。这与参照项目 DeerFlow 是同一个设计——一个端点，
 * 只靠这个参数切换 `Content-Disposition`。
 */
export function outputFileUrl(threadId: string, path: string, download = false): string {
  const relative = path.replace(/^\/output\//, "");
  const encoded = relative.split("/").map(encodeURIComponent).join("/");
  const url = `${API_URL}/threads/${encodeURIComponent(threadId)}/files/${encoded}`;
  return download ? `${url}?download=true` : url;
}
