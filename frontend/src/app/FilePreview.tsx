"use client";

import { useEffect, useState } from "react";
import { outputFileUrl } from "@/core/api/config";
import { PresentedFileView } from "@/core/chat/useChat";
import { fenceFor, formatSize, hljsLanguage, isTruncated, previewKind, typeLabel } from "@/core/preview";
import Markdown from "./Markdown";

/**
 * 交付文件的内容预览——`/app` 网格的第三个格子（真列，对话会因此收窄而不是被盖住）。
 *
 * **取内容用的是与下载同一个地址**，只是不带 `?download=true`（见 `outputFileUrl`）。
 * 这与参照项目 DeerFlow 是同一个设计：一个端点，靠那个参数切换 `Content-Disposition`，
 * 所以后端不必为预览再开一条接口。
 *
 * **代码复用 `Markdown` 组件**：把文本包成一个围栏代码块再交给它，高亮、横向滚动、暗色
 * 样式全部免费拿到，不必为预览另写一套代码渲染。围栏长度由 `fenceFor` 按内容里最长的一段
 * 反引号算出来——文件本身就可能包含 ``` （Markdown 文件、文档里的示例），等长的围栏会把
 * 代码块提前闭合。
 */

const MAX_PREVIEW_BYTES = 256 * 1024;
/** 预览取多少字节。比 DeerFlow 的 1 MiB 保守：本地助手的交付物不该动辄几百 KB。 */

type Loaded =
  | { state: "loading" }
  | { state: "ready"; text: string; truncated: boolean }
  | { state: "failed" };

function asFencedCode(text: string, name: string): string {
  const fence = fenceFor(text);
  // 认不出的后缀就不写语言：`rehype-highlight` 拿到不认识的标识是自找麻烦。
  const language = hljsLanguage(name) ?? "";
  return `${fence}${language}\n${text}\n${fence}`;
}

function Fallback({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <div className="preview-note">
      <p>{children}</p>
      <a className="file-download" href={href}>
        下载
      </a>
    </div>
  );
}

export default function FilePreview({
  file,
  threadId,
  onClose,
}: {
  file: PresentedFileView;
  threadId: string;
  onClose: () => void;
}) {
  const kind = previewKind(file.name, file.mime);
  const inlineUrl = outputFileUrl(threadId, file.path);
  const downloadUrl = outputFileUrl(threadId, file.path, true);
  const [loaded, setLoaded] = useState<Loaded>({ state: "loading" });
  const [imageFailed, setImageFailed] = useState(false);

  useEffect(() => {
    if (kind !== "code" && kind !== "markdown") {
      return;
    }
    let alive = true;
    setLoaded({ state: "loading" });
    void (async () => {
      try {
        const response = await fetch(inlineUrl, {
          cache: "no-store",
          // 只取前 N 字节。服务端不认 Range 时会直接给全文，所以下面还要自己兜一层截断。
          headers: { Range: `bytes=0-${MAX_PREVIEW_BYTES - 1}` },
        });
        if (!response.ok) {
          throw new Error(String(response.status));
        }
        // 按**字节**而不是字符来算：上限是字节数，而一个中文字符占 3 字节——拿字符数当
        // 字节数会让中文文件的截断判断整体偏移三倍。
        const bytes = new Uint8Array(await response.arrayBuffer());
        if (!alive) {
          return;
        }
        const truncated = isTruncated(response.headers.get("content-range"), bytes.byteLength, MAX_PREVIEW_BYTES);
        setLoaded({
          state: "ready",
          text: new TextDecoder().decode(bytes.subarray(0, MAX_PREVIEW_BYTES)),
          truncated,
        });
      } catch {
        if (alive) {
          setLoaded({ state: "failed" });
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [inlineUrl, kind]);

  return (
    <aside className="file-preview">
      <div className="panel-head">
        <h2 className="preview-name" title={file.path}>
          {file.name}
        </h2>
        <div className="panel-head-right">
          <a className="file-download" href={downloadUrl}>
            下载
          </a>
          <button className="panel-close" type="button" onClick={onClose} title="关闭预览">
            ×
          </button>
        </div>
      </div>
      <div className="preview-meta">
        {typeLabel(file.name)} · {formatSize(file.size)}
      </div>

      <div className="preview-body">
        {kind === "image" &&
          (imageFailed ? (
            <Fallback href={downloadUrl}>这张图片读不出来。</Fallback>
          ) : (
            <img
              className="preview-image"
              src={inlineUrl}
              alt={file.name}
              onError={() => setImageFailed(true)}
            />
          ))}

        {kind === "none" && (
          <Fallback href={downloadUrl}>这个类型没法在浏览器里预览。</Fallback>
        )}

        {(kind === "code" || kind === "markdown") && loaded.state === "loading" && (
          <div className="preview-note">
            <p>加载中…</p>
          </div>
        )}

        {(kind === "code" || kind === "markdown") && loaded.state === "failed" && (
          <Fallback href={downloadUrl}>取不到这个文件的内容。</Fallback>
        )}

        {(kind === "code" || kind === "markdown") && loaded.state === "ready" && (
          <>
            {loaded.truncated && (
              <div className="preview-banner">
                文件较大，只显示了前 {formatSize(MAX_PREVIEW_BYTES)}。完整内容请下载。
              </div>
            )}
            {kind === "markdown" ? (
              <Markdown text={loaded.text} />
            ) : (
              <Markdown text={asFencedCode(loaded.text, file.name)} />
            )}
          </>
        )}
      </div>
    </aside>
  );
}
