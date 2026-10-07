/** `preview.ts` 与 `api/config.ts` 里那两个纯函数的契约测试。
 *
 * 它们判错的后果都是**静默**的：类型分派错了只是「这个文件不能预览」，URL 拼错了只是
 * 请求 404——没有一处会报错，只会看起来像功能没做。
 */

import { describe, expect, it } from "vitest";
import { API_URL, outputFileUrl } from "./api/config";
import { fenceFor, hljsLanguage, isTruncated, previewKind, suffixOf } from "./preview";

describe("previewKind", () => {
  it("按后缀认出图片", () => {
    expect(previewKind("chart.png", "image/png")).toBe("image");
    expect(previewKind("photo.JPEG", null)).toBe("image");
  });

  it("svg 不算可预览的图片", () => {
    // 它能带脚本；我们的下载路由也把 svg 归进「强制附件」。两处口径一致。
    expect(previewKind("icon.svg", "image/svg+xml")).toBe("none");
  });

  it("md 走 Markdown 渲染，不是当纯文本", () => {
    expect(previewKind("报告.md", "text/markdown")).toBe("markdown");
    expect(previewKind("notes.markdown", null)).toBe("markdown");
  });

  it("后端认不出 MIME 的代码文件仍判成代码", () => {
    // 实测 `mimetypes.guess_type("quicksort.go")` 返回 None——只看 MIME 的话，
    // 代码文件会全被判成「不支持预览」。这条钉住「后缀优先」这个判据。
    expect(previewKind("quicksort.go", null)).toBe("code");
    expect(previewKind("main.rs", null)).toBe("code");
  });

  it("text/* 与那几个非 text 的文本 MIME 也算代码", () => {
    expect(previewKind("notes", "text/plain")).toBe("code");
    expect(previewKind("data", "application/json")).toBe("code");
  });

  it("认不出来的落到 none，不冒充文本", () => {
    expect(previewKind("archive.zip", "application/zip")).toBe("none");
    expect(previewKind("report.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")).toBe("none");
    expect(previewKind("README", null)).toBe("none");
  });

  it("点开头的文件名不被当成扩展名", () => {
    expect(suffixOf(".gitignore")).toBe("");
    expect(previewKind(".gitignore", null)).toBe("none");
  });
});

describe("hljsLanguage", () => {
  it("映射到 hljs common 集里真实存在的名字", () => {
    expect(hljsLanguage("quicksort.go")).toBe("go");
    expect(hljsLanguage("main.py")).toBe("python");
  });

  it("tsx/jsx 落到 typescript/javascript，而不是照抄后缀", () => {
    // highlight.js 的 common 集里没有 `tsx` / `jsx` 这两个名字，照抄会让它拿到一个
    // 不认识的标识。
    expect(hljsLanguage("App.tsx")).toBe("typescript");
    expect(hljsLanguage("App.jsx")).toBe("javascript");
  });

  it("认不出来时返回 null——围栏就不写语言", () => {
    expect(hljsLanguage("data.csv")).toBeNull();
    expect(hljsLanguage("README")).toBeNull();
  });
});

describe("fenceFor", () => {
  it("用最短的合法围栏", () => {
    expect(fenceFor("package main")).toBe("```");
  });

  it("比内容里最长的一段反引号还长", () => {
    // 内容里本来就有 ``` 时，等长的围栏会把代码块提前闭合。
    expect(fenceFor("看这段：\n```go\nx\n```\n")).toBe("````");
    expect(fenceFor("``````")).toBe("```````");
  });
});

describe("isTruncated", () => {
  const LIMIT = 256 * 1024;

  it("小文件不算截断——哪怕状态码是 206", () => {
    // 这是真踩过的坑：FileResponse 对任何合法的 Range 请求都回 206，100 字节的文件 +
    // `bytes=0-262143` 也是 206 加全文。拿状态码当判据，每个小文件都会顶着一句
    // 「文件较大，只显示了前 256 KB」。
    expect(isTruncated("bytes 0-99/100", 100, LIMIT)).toBe(false);
  });

  it("恰好等于上限也不叫截断（全拿到了）", () => {
    expect(isTruncated(`bytes 0-${LIMIT - 1}/${LIMIT}`, LIMIT, LIMIT)).toBe(false);
  });

  it("总数大于收到的字节数 = 截断了", () => {
    expect(isTruncated(`bytes 0-${LIMIT - 1}/300000`, LIMIT, LIMIT)).toBe(true);
  });

  it("服务端不理 Range（没有 Content-Range）时按收到的长度兜底", () => {
    expect(isTruncated(null, 1024, LIMIT)).toBe(false);
    expect(isTruncated(null, LIMIT * 2, LIMIT)).toBe(true);
  });

  it("头的形状认不出来时不崩，退回兜底判断", () => {
    expect(isTruncated("nonsense", 1024, LIMIT)).toBe(false);
  });
});

describe("outputFileUrl", () => {
  it("默认是 inline（给预览取内容用），download=true 才是下载", () => {
    expect(outputFileUrl("t1", "/output/a.md")).not.toContain("download=true");
    expect(outputFileUrl("t1", "/output/a.md", true)).toContain("download=true");
  });

  it("摘掉 /output 前缀，逐段编码但保留分隔符", () => {
    // 后端路由的形状是 /threads/{会话}/files/{相对路径}，允许带子目录。
    expect(outputFileUrl("t1", "/output/reports/2026/q1.md")).toBe(
      `${API_URL}/threads/t1/files/reports/2026/q1.md`,
    );
  });

  it("中文名与需要转义的字符被逐段编码", () => {
    expect(outputFileUrl("t1", "/output/报告.md")).toBe(
      `${API_URL}/threads/t1/files/%E6%8A%A5%E5%91%8A.md`,
    );
    expect(outputFileUrl("t1", "/output/a b/c#d.txt")).toBe(
      `${API_URL}/threads/t1/files/a%20b/c%23d.txt`,
    );
  });

  it("路径里没有 /output/ 前缀时不把文件名吃掉", () => {
    // 防御性：事件里给的永远是 /output/xxx，但真出现别的形状也不该拼出一个坏 URL。
    expect(outputFileUrl("t1", "a.md")).toBe(`${API_URL}/threads/t1/files/a.md`);
  });
});
