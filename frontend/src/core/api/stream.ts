/** 手写 SSE 解析器（照搬 langgraph-sdk SSEDecoder 思路，不引入 langgraph-sdk）。

 * 服务端 SSE 帧格式（backend/app/gateway/sse.py 的 format_sse）：
 *   event: <name>\n
 *   data: <json>\n
 *   \n
 */

export interface SSEEvent {
  event: string;
  data: unknown;
}

function parseFrame(frame: string): SSEEvent | null {
  let event = "message";
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) {
      event = line.slice("event:".length).trim();
    } else if (line.startsWith("data:")) {
      data += line.slice("data:".length).trim();
    }
  }
  if (data === "") return null;
  let parsed: unknown = data;
  try {
    parsed = JSON.parse(data);
  } catch {
    // 非 JSON 的 data 原样返回
  }
  return { event, data: parsed };
}

export async function* parseSSE(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<SSEEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep = buffer.indexOf("\n\n");
    while (sep !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      const evt = parseFrame(frame);
      if (evt) yield evt;
      sep = buffer.indexOf("\n\n");
    }
  }
}
