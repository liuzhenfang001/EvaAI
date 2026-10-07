"use client";

import { outputFileUrl } from "@/core/api/config";
import { PresentedFileView } from "@/core/chat/useChat";
import { formatSize, typeLabel } from "@/core/preview";

/**
 * 助手这一轮交付给用户的文件（T11，来自 `files_presented` 事件）。
 *
 * 数据是**事件投影出来的**，不是扫目录扫出来的——所以刷新页面从 journal 回放时，卡片
 * 与实时看到的完全一致（走同一个 reducer），不需要另写一套历史恢复逻辑。
 */

export default function FileCards({
  files,
  threadId,
  onPreview,
}: {
  files: PresentedFileView[];
  threadId: string | null;
  onPreview: (file: PresentedFileView) => void;
}) {
  // 没有会话 id 就拼不出下载地址（地址是会话作用域的）。与其给一个点不动的链接，不如不显示。
  if (!threadId || files.length === 0) {
    return null;
  }

  return (
    <ul className="files">
      {files.map((file, index) => (
        // 键带上序号：模型可以在同一轮里把同一个文件交两次，光用路径会撞键。
        <li key={`${index}:${file.path}`} className="file">
          <span className="file-icon" aria-hidden="true">
            📄
          </span>
          {/* 「看内容」做成一整块按钮，而不是把整张卡片包起来——卡片里已经有一个下载
              链接了，按钮套链接既是非法结构，键盘 Tab 的顺序也会打架。 */}
          <button className="file-info" type="button" onClick={() => onPreview(file)} title={`预览 ${file.path}`}>
            <span className="file-name">{file.name}</span>
            <span className="file-meta">
              {typeLabel(file.name)} · {formatSize(file.size)}
            </span>
          </button>
          <a className="file-download" href={outputFileUrl(threadId, file.path, true)}>
            下载
          </a>
        </li>
      ))}
    </ul>
  );
}
