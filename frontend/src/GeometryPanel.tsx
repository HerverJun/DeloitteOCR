import { useEffect, useState } from "react";
import { Button, Select } from "@fluentui/react-components";
import { api } from "./api";
import type { Result, Task, Version } from "./types";
import { RegionPreview, type Location } from "./RegionPreview";
export type GeometryTarget = { kind: "cell"; table: number; row: number; column: number } | { kind: "text"; start: number; end: number };
type Evidence = { id: string; target: GeometryTarget; polygon: number[][] | null; source: string; details: { level: string; reason: string; display_reason?: string; range_semantics?: string; table_polygon?: number[][] } };
const locationOf = (item: Evidence | undefined, version: Version): Location => ({
  level: item?.details.level || "image", polygon: item?.polygon || null, version_id: version.id,
  range_semantics: item?.details.range_semantics,
  reason: item?.details.display_reason || (item?.source === "manual" ? "人工定位" : item?.details.level === "cell" ? "单元格定位（实验性）" : "没有可靠单元格对应，显示表格区域或全图"),
});
export function GeometryPanel({ result, version, target, tasks, refreshKey, disabled, onPrepare, onBind, onLocate, onUpdated }: {
  result: Result; version: Version; target: GeometryTarget | null; tasks: Task[]; refreshKey: number; disabled: boolean;
  onPrepare: () => Promise<Result>; onBind: (target: GeometryTarget) => void; onLocate: (location: Location) => void; onUpdated: () => void;
}) {
  const [evidence, setEvidence] = useState<Evidence[]>([]), [error, setError] = useState("");
  const [working, setWorking] = useState(false), [taskId, setTaskId] = useState("");
  const [message, setMessage] = useState("");
  const [provider, setProvider] = useState("paddle"), [algorithm, setAlgorithm] = useState("local-v2");
  const task = tasks.find(t => t.id === taskId);
  useEffect(() => {
    let alive = true;
    api<{ evidence: Evidence[] }>(`/results/${result.id}/geometry/location`, "POST", { target, provider, algorithm }).then(r => {
      if (!alive) return;
      setEvidence(r.evidence);
      const item = r.evidence[0];
      if (target) onLocate(locationOf(item, version));
    }).catch(e => { if (alive) setError(String(e)); });
    return () => { alive = false; };
  }, [result.id, result.revision, version.id, JSON.stringify(target), task?.status, refreshKey, provider, algorithm]);
  useEffect(() => { if (task?.status === "succeeded") { setMessage(task.phase || "定位完成"); onUpdated(); } }, [task?.status]);
  const compute = async (force: boolean) => {
    setWorking(true); setError("");
    try {
      const current = await onPrepare();
      const answer = await api(`/results/${current.id}/geometry`, "POST", { revision: current.revision, force, provider, algorithm });
      setTaskId(answer.task_id || ""); setMessage(answer.cached ? "已使用缓存定位" : "定位任务已加入队列"); onUpdated();
    } catch (e) { setError(String(e)); } finally { setWorking(false); }
  };
  const first = evidence[0];
  const location = locationOf(first, version);
  return <details className="geometry-panel" open={!!target}>
    <summary>表格定位与人工绑定 · 实验性</summary>
    <p>选中表格单元格，或在文字编辑区选中一段文字，再框选原图绑定。结构修改后需要重新定位。</p>
    <div className="geometry-actions">
      <label>定位模型 <Select size="small" value={provider} disabled={disabled || working} onChange={(_, data) => { setProvider(data.value); setMessage(""); setTaskId(""); }}>
        <option value="paddle">Paddle（默认）</option><option value="tableformer-raw">TableFormer raw（实验）</option><option value="rapidtable">RapidTable（对照）</option>
      </Select></label>
      <label>对应方式 <Select size="small" value={algorithm} disabled={disabled || working} onChange={(_, data) => { setAlgorithm(data.value); setMessage(""); setTaskId(""); }}>
        <option value="local-v2">现有局部对应</option><option value="local-v3">邻居锚点对应（实验）</option><option value="local-v4">复杂表格空间对应（实验）</option>
      </Select></label>
    </div>
    <div className="geometry-actions"><Button size="small" disabled={disabled || working} onClick={() => void compute(false)}>补充表格定位</Button>
      <Button size="small" disabled={disabled || working} onClick={() => void compute(true)}>重新定位</Button>
      <Button size="small" disabled={!target || working} onClick={() => target && onBind(target)}>人工框选绑定</Button></div>
    {message && <p role="status">{task ? task.phase : message}</p>}{task?.error && <p role="alert">{String(task.error)}</p>}{error && <p role="alert">{error}</p>}
    {target && <><p>{target.kind === "cell" ? `表 ${target.table+1} · 第 ${target.row+1} 行 / 第 ${target.column+1} 列` : `已选文字第 ${target.start+1}–${target.end} 字符`}</p>
      <RegionPreview version={version} location={location} tablePolygon={first?.details.table_polygon} /></>}
  </details>;
}
