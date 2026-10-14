import { useEffect, useState } from "react";
import { Button } from "@fluentui/react-components";
import { api, request } from "./api";
import { downloadAgentArtifact } from "./agentApi";

type Artifact = { state: string; pinned: number; expires: number; bytes: number | null; manifest: { results: unknown[]; coverage: { failed_pages?: unknown[] } } };
const names: Record<string, string> = { ready: "导出已保存", staging: "正在创建导出", expired: "产物已过期", deleted: "产物已删除", missing: "产物文件缺失", corrupt: "产物校验失败", deleting: "正在删除产物", expiring: "正在清理过期产物" };

export function AgentArtifact({ projectId, id, busy, action }: { projectId: string; id: string; busy: boolean; action: (work: () => Promise<void>) => Promise<void> }) {
  const path = `/projects/${encodeURIComponent(projectId)}/agent/artifacts/${encodeURIComponent(id)}`;
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    void request(path, { signal: controller.signal }).then(r => r.json()).then(value => {
      if (!controller.signal.aborted) setArtifact(value);
    }).catch(e => { if (!controller.signal.aborted) setError(String(e)); });
    return () => controller.abort();
  }, [path]);
  const expired = !!artifact && !artifact.pinned && artifact.expires * 1000 <= Date.now();
  const ready = artifact?.state === "ready" && !expired;
  const partial = !!artifact?.manifest.coverage.failed_pages?.length;
  return <div className="agent-tool"><strong>{artifact ? expired && artifact.state === "ready" ? "产物已过期" : names[artifact.state] || "产物状态待核对" : "正在读取产物状态"}</strong>
    {error && <p role="alert">{error}</p>}
    {artifact && <p>{artifact.manifest.results.length} 份结果 · {artifact.bytes == null ? "大小待确认" : `${Math.ceil(artifact.bytes / 1024)} KB`}{partial ? " · 部分导出，请同时下载覆盖清单核对未覆盖页" : ""}<br />{artifact.pinned ? "已保留，不自动清理" : `保留至 ${new Date(artifact.expires * 1000).toLocaleDateString()}`}</p>}
    <Button size="small" disabled={busy || !ready} onClick={() => void action(() => downloadAgentArtifact(projectId, id))}>下载导出文件</Button>
    {partial && <Button size="small" disabled={busy || !ready} onClick={() => void action(() => downloadAgentArtifact(projectId, id, true))}>下载覆盖清单</Button>}
    {artifact?.state === "ready" && <Button size="small" appearance="subtle" disabled={busy} onClick={() => void action(async () => {
      setArtifact(await api(path, "PATCH", { pinned: !artifact.pinned }));
    })}>{artifact.pinned ? "取消保留" : "保留产物"}</Button>}
    {artifact && !artifact.pinned && ["ready", "missing", "corrupt"].includes(artifact.state) && <details><summary>清理此产物</summary><p>删除已生成的文件，原识别结果保留。活跃运行或下载中的文件暂不能删除。</p><Button size="small" disabled={busy} onClick={() => void action(async () => {
      await api(path, "DELETE"); setArtifact({ ...artifact, state: "deleted" });
    })}>删除此导出文件</Button></details>}
  </div>;
}
