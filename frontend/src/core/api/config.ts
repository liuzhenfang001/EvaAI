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
