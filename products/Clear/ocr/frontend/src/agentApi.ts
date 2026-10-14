import { request } from "./api";
import type { AgentEvent } from "./agentTypes";

export const agentRequestId = () => crypto.randomUUID();

export async function downloadAgentArtifact(projectId: string, artifactId: string, coverageManifest = false) {
  const response = await request(`/projects/${encodeURIComponent(projectId)}/agent/artifacts/${encodeURIComponent(artifactId)}/${coverageManifest ? "coverage-manifest" : "download"}`);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] || "OCR-export";
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function readAgentStream(sessionId: string, after: number, signal: AbortSignal, receive: (event: AgentEvent) => void, onOpen?: () => void) {
  const response = await request(`/agent/sessions/${encodeURIComponent(sessionId)}/stream?after_seq=${after}`, { signal });
  if (!response.body) throw Error("浏览器不支持事件流，请刷新重试");
  if (signal.aborted) return;
  onOpen?.();
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      if (buffer.length > 256 * 1024) throw Error("事件流超过缓冲上限，请重新连接");
      let end: number;
      while ((end = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, end); buffer = buffer.slice(end + 2);
        const data = block.split("\n").filter(line => line.startsWith("data: ")).map(line => line.slice(6)).join("\n");
        if (data) {
          const event = JSON.parse(data) as AgentEvent;
          if (event.session_id !== sessionId || !Number.isSafeInteger(event.seq) || event.seq <= 0) throw Error("事件序列无效");
          receive(event);
        }
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
