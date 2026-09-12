import { useEffect, useMemo, useRef, useState } from "react";
import { Button } from "@fluentui/react-components";
import { api } from "./api";
import { engineNames, type Engine, type Task } from "./types";
import "./fusion.css";

export type FusionRequest = { content_type: string; mode: string; engines: string[]; result_ids: string[]; reuse: boolean; request_id: string };
export function FusionLauncher({ engines, tasks, versionId, selectedCount, disabled, recognitionDisabled, onStart }: {
  engines: Record<string, Engine>; tasks: Task[]; versionId: string | undefined; selectedCount: number;
  disabled: boolean; recognitionDisabled: boolean; onStart: (request: FusionRequest) => Promise<void>;
}) {
  const [kind, setKind] = useState("table");
  const [mode, setMode] = useState("conservative");
  const [chosen, setChosen] = useState<string[]>(["paddlevl", "glm", "hunyuan"]);
  const [batch, setBatch] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [policies, setPolicies] = useState<Record<string, Record<string, { automatic_replacement: boolean }>>>({});
  const request = useRef<FusionRequest | null>(null);
  const details = useRef<HTMLDetailsElement>(null);
  const batches = useMemo(() => {
    const sameVersion = new Set(tasks.filter(t => t.kind !== "fusion" && t.version_id === versionId && t.batch).map(t => t.batch!));
    return [...sameVersion].reverse().map(id => ({ id, tasks: tasks.filter(t => t.batch === id && (!t.kind || t.kind === "ocr")) }));
  }, [tasks, versionId]);
  const activeBatch = batches.find(b => b.id === batch) || batches[0];
  const sourceTasks = activeBatch?.tasks.filter(t => chosen.includes(t.engine)) || [];
  const reusable = !!chosen.length && chosen.every(engine => sourceTasks.some(t => t.engine === engine)) &&
    sourceTasks.every(t => ["succeeded", "failed", "cancelled"].includes(t.status)) && sourceTasks.some(t => t.result_id);
  useEffect(() => { void api<typeof policies>("/fusion/policies").then(setPolicies).catch(e => setError(String(e))); }, []);
  const start = async (reuse: boolean) => {
    if (working) return;
    setWorking(true); setError("");
    const value: FusionRequest = { content_type: kind, mode, engines: chosen,
      result_ids: reuse ? sourceTasks.filter(t => t.status === "succeeded" && t.result_id).map(t => t.result_id!) : [],
      reuse, request_id: "" };
    const previous = request.current;
    value.request_id = previous && JSON.stringify({ ...previous, request_id: "" }) === JSON.stringify(value) ? previous.request_id : crypto.randomUUID();
    request.current = value;
    try { await onStart(value); request.current = null; if (details.current) details.current.open = false; }
    catch (e) { setError(String(e)); }
    finally { setWorking(false); }
  };
  return <details ref={details} className="fusion-launch"><summary>融合识别 · 生成有来源的校对草稿</summary>
    <div className="fusion-settings">
      <label>内容 <select aria-label="融合内容类型" value={kind} disabled={working} onChange={e => { setKind(e.target.value); setChosen(Object.keys(engines).filter(k => e.target.value !== "table" || engines[k].capabilities.tables)); }}><option value="table">表格</option><option value="print">印刷体文字</option><option value="handwriting">手写文字</option></select></label>
      <label>策略 <select aria-label="融合策略" value={mode} disabled={working} onChange={e => setMode(e.target.value)}><option value="conservative">保守（默认）</option><option value="aggressive">积极</option></select></label>
    </div>
    <div className="fusion-engines" role="group" aria-label="参与融合的引擎">{Object.entries(engines).map(([id, engine]) => <label key={id}><input type="checkbox" checked={chosen.includes(id)} disabled={working || (kind === "table" && !engine.capabilities.tables)} onChange={e => setChosen(old => e.target.checked ? [...old, id] : old.filter(k => k !== id))} />{engineNames[id]}{kind === "table" && !engine.capabilities.tables ? "（无表格结构）" : ""}</label>)}</div>
    <p className="fusion-muted">{policies[kind]?.[mode]?.automatic_replacement ? "按公开数据验证后的策略生成草稿，最终由你确认。" : "此类别目前保留基准并提供候选建议，自动替换尚未开放。"} 新结果先进入预览，采用后才成为正式校对与导出目标。</p>
    <div className="fusion-settings">
      <label>当前图片批次 <select aria-label="复用融合批次" value={activeBatch?.id || ""} disabled={working || !batches.length} onChange={e => setBatch(e.target.value)}>{!batches.length && <option value="">暂无兼容批次</option>}{batches.map((b, i) => <option value={b.id} key={b.id}>批次 {batches.length-i} · {b.tasks.filter(t => t.result_id).length}/{b.tasks.length} 份成功</option>)}</select></label>
      <Button disabled={disabled || working || !reusable} onClick={() => void start(true)}>复用当前图片结果</Button>
      <Button appearance="primary" disabled={disabled || working || recognitionDisabled || !chosen.length || !versionId} onClick={() => void start(false)}>完整识别并融合 · {selectedCount || 1} 张</Button>
    </div>
    {recognitionDisabled && <p className="fusion-muted">当前不能新增 OCR；兼容的已有结果仍可在 CPU 上融合。</p>}
    {error && <p className="inline-warning" role="alert">{error}</p>}
  </details>;
}
