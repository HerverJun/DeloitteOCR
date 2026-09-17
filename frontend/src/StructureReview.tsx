import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@fluentui/react-components";
import { api } from "./api";
import type { Result, Table, Version } from "./types";
import { RegionPreview, type Location } from "./RegionPreview";
import { ReviewClock } from "./reviewClock";
import "./structure.css";

type Difference = { kind: string; row?: number; column?: number; current?: unknown; candidate?: unknown };
export type StructureProposal = {
  id: string; basis: string; revision: number; version_id: string; state: string;
  kind: string; table_indices: number[]; current_tables: Table[]; proposed_tables: Table[];
  differences: Difference[]; conflicts: { kind: string; text?: string; row?: number; column?: number; token_ids?: string[]; reasons?: string[] }[];
  provider: string; can_apply: boolean; polygon: number[][] | null; token_pool_sha256?: string;
  model_versions?: string[]; text_source_result?: string; unassigned_tokens?: { id: string; raw_text: string }[];
  retained_values?: { manual: boolean }[]; unverified_empty_cells?: { row: number; column: number }[];
};
type StructureView = { revision: number; adopted: boolean; candidates: { id: string; provider: string; tables: number }[]; proposals: StructureProposal[];
  table_tool?: {state:string;message:string;error?:string;retry_allowed:boolean} };
const kinds: Record<string,string> = { replace_table: "整表候选", merge: "局部合并", split: "局部拆分", insert_rows: "补行", insert_columns: "补列", header: "表头修正", table_identity: "表身份核对", native_table: "原生文字建表", split_tables: "拆为多表", merge_tables: "合并同页表" };
const states: Record<string,string> = { pending: "待复核", deferred: "已暂缓", accepted: "已接受", kept: "保留当前", rejected: "已拒绝" };
const providerName = (provider: string) => provider.startsWith("pdfplumber/") ? `PDF 表格工具 · 区域 ${Number(provider.split(":").pop())+1}` : provider;
const reasons: Record<string,string> = { missing_rows: "候选增加行", extra_rows: "候选减少行", missing_columns: "候选增加列", extra_columns: "候选减少列", span: "合并跨度不同", cell_partition: "单元格划分不同", header: "表头关系不同", table_identity: "表格数量或身份待核对", unassigned_tokens: "文字尚未归属", rejected_source_tokens: "部分来源文字缺少可靠坐标", invalid_candidate: "候选结构不完整或处理超时", manual_value_unmapped: "人工修订没有唯一对应格", current_value_unmapped: "当前文字没有可靠对应格", table_identity_ambiguous: "表身份尚不可靠" };

function TablePreview({ table, label }: { table: Table; label: string }) {
  const rows = new Map<number, typeof table.cells>();
  table.cells.forEach(cell => rows.set(cell.row, [...(rows.get(cell.row) || []), cell]));
  const occupied = new Set(table.cells.flatMap(cell => Array.from({length:cell.row_span},(_,r) =>
    Array.from({length:cell.column_span},(_,c) => `${cell.row+r}:${cell.column+c}`)).flat()));
  const missing = (row: number) => Array.from({length:table.columns},(_,column) => column)
    .filter(column => !occupied.has(`${row}:${column}`));
  return <div className="structure-table"><strong>{label} · {table.rows} 行 × {table.columns} 列</strong>
    <div className="structure-table-scroll"><table aria-label={label}><tbody>{Array.from({ length: table.rows }, (_, row) => <tr key={row}>
      {[...(rows.get(row) || []),...missing(row).map(column => ({row,column,row_span:1,column_span:1,text:'缺失格位',is_header:false,structure_source:undefined}))].sort((a,b) => a.column-b.column).map(cell => <td key={cell.column} rowSpan={cell.row_span} colSpan={cell.column_span}
        data-header={cell.is_header || undefined} data-empty={cell.structure_source?.text_state === "unverified_empty" || undefined}
        title={`第 ${cell.row+1} 行，第 ${cell.column+1} 列；跨 ${cell.row_span} 行、${cell.column_span} 列`}>
        {cell.text || (cell.structure_source?.text_state === "unverified_empty" ? "待核对空值" : "（空）")}</td>)}
    </tr>)}</tbody></table></div></div>;
}

export function StructureReview({ result, version, busy, refreshKey, focusId, onPrepare, onDecision, getPendingDecision, onUpdated, onLocate, onManual }:
  { result: Result; version: Version; busy: boolean; refreshKey: number; focusId?: string;
    onPrepare: () => Promise<Result>; onDecision: (id: string, body: Record<string,unknown>) => Promise<unknown>;
    getPendingDecision: () => { issueId: string; body: Record<string,unknown> } | null;
    onUpdated: () => void; onLocate: (location: Location) => void; onManual: (table: number, row: number, column: number) => void }) {
  const [view, setView] = useState<StructureView | null>(null);
  const [selected, setSelected] = useState("");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [showResolved, setShowResolved] = useState(false);
  const lock = useRef(false), clock = useRef(new ReviewClock());
  const pending = getPendingDecision();
  const retry = pending?.body.decision_kind === "structure" ? pending : null;
  const proposals = (view?.proposals || []).filter(p => showResolved || ["pending","deferred"].includes(p.state));
  const active = proposals.find(p => p.id === selected) || proposals[0];
  const ready = useCallback((value: boolean) => clock.current.setReady(value), []);
  const load = useCallback(async () => { const value = await api<StructureView>(`/results/${result.id}/structure`); setView(value); }, [result.id]);
  useEffect(() => {
    if (!['queued','running'].includes(view?.table_tool?.state || '')) return;
    let active = true;
    const timer = setInterval(() => { api<StructureView>(`/results/${result.id}/structure`).then(value => {
      if (active) setView(value);
    }).catch(e => { if (active) setError(String(e)); }); }, 1000);
    return () => { active = false; clearInterval(timer); };
  }, [result.id, view?.table_tool?.state]);
  useEffect(() => { let valid = true; api<StructureView>(`/results/${result.id}/structure`).then(v => { if (valid) setView(v); }).catch(e => { if (valid) setError(String(e)); }); return () => { valid = false; }; }, [result.id, result.revision, refreshKey]);
  useEffect(() => { if (focusId) setSelected(focusId); }, [focusId]);
  useEffect(() => { setAcknowledged(false); clock.current.reset(); }, [active?.id]);
  useEffect(() => { clock.current.setWaiting(busy || working); }, [busy,working]);
  useEffect(() => {
    const c = clock.current, timer = setInterval(() => c.tick(), 1000);
    document.addEventListener("visibilitychange",c.visibility); window.addEventListener("pointerdown",c.interact); window.addEventListener("keydown",c.interact); window.addEventListener("pointermove",c.interact);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange",c.visibility); window.removeEventListener("pointerdown",c.interact); window.removeEventListener("keydown",c.interact); window.removeEventListener("pointermove",c.interact); };
  }, []);
  const run = async (work: () => Promise<unknown>) => {
    if (lock.current) return;
    lock.current = true; setWorking(true); setError("");
    try { await work(); } catch(e) { setError(String(e)); } finally { lock.current = false; setWorking(false); }
  };
  const decide = (action: string) => run(async () => {
    if (!active && !retry) return;
    const target = retry?.issueId || active!.id;
    const body = retry?.body || { decision_kind: "structure", request_id: crypto.randomUUID(), action,
      revision: active!.revision, basis: active!.basis, version_id: active!.version_id, acknowledge_unverified_empty: acknowledged };
    clock.current.tick();
    await onDecision(target,body);
    try {
      const saved = await api<Result>(`/results/${result.id}`);
      await api(`/results/${result.id}/review-timing`,"POST",{ id:body.request_id, revision:saved.revision,
        target:{kind:"structure",proposal_id:target}, active_ms:Math.round(clock.current.activeMs), action:`structure_${body.action}` });
    } catch { /* The durable decision remains successful if timing is unavailable. */ }
    await load(); onUpdated();
  });
  const disabled = busy || working || !!pending || !view?.adopted;
  const location: Location = { level: active?.polygon ? "region" : "image", polygon: active?.polygon || null, version_id: version.id, reason: "结构建议对应区域" };
  return <section className="structure-review" aria-label="结构建议与复核">
    <div className="structure-heading"><strong>结构建议与复核</strong><span>实验性 · 人工确认</span></div>
    <p>对照原图检查行列、合并格和表头。候选使用来源文字填充；空值仍需核对。</p>
    {view?.table_tool && view.table_tool.state !== 'not_applicable' && <div role="status" aria-label="PDF 表格提取状态">
      <p>{view.table_tool.message}</p>
      {view.table_tool.error && <p className="inline-warning">{view.table_tool.error}</p>}
      {view.table_tool.retry_allowed && <Button size="small" disabled={busy || working || !!pending} onClick={() => void run(async () => {
        const saved = await onPrepare();
        await api(`/results/${result.id}/structure/tool-retry`,"POST",{revision:saved.revision});
        await load(); onUpdated();
      })}>重新提取 PDF 表格</Button>}
    </div>}
    <div className="structure-actions"><Button size="small" disabled={busy || working || !!pending} onClick={() => void run(async () => {
      const saved = await onPrepare(); setView(await api<StructureView>(`/results/${result.id}/structure/check`,"POST",{ revision:saved.revision })); onUpdated();
    })}>检查已有候选</Button><label><input type="checkbox" checked={showResolved} disabled={working || !!pending} onChange={e => setShowResolved(e.target.checked)} />显示已处理</label></div>
    {!view?.candidates.length && <p className="structure-empty">尚无结构候选。可在下方「表格辅助定位」运行提供方，完成后检查候选。</p>}
    {!!view?.candidates.length && <p className="structure-source">已有来源：{[...new Set(view.candidates.map(c => c.provider.startsWith("pdfplumber/") ? "PDF 表格工具" : c.provider))].join("、")}</p>}
    {error && <p className="inline-warning" role="alert">{error}</p>}
    {retry && <p className="inline-warning">上次结构决策待核实，重试会使用原请求。<Button disabled={busy || working} onClick={() => void decide("retry")}>重试结构决策</Button></p>}
    {!view?.adopted && <p>先采用此结果，再提交复核决策。</p>}
    {active ? <>
      <select aria-label="结构建议" value={active.id} disabled={working || !!pending} onChange={e => setSelected(e.target.value)}>{proposals.map(p => <option key={p.id} value={p.id}>
        {p.table_indices.length ? `表 ${p.table_indices.map(i => i+1).join("、")}` : "页面"} · {kinds[p.kind] || p.kind} · {providerName(p.provider)} · {states[p.state]}
      </option>)}</select>
      <div className="structure-actions"><Button size="small" onClick={() => onLocate(location)}>定位原图</Button><Button size="small" disabled={busy || working || !!pending} onClick={() => onManual(active.table_indices[0] || 0,active.differences[0]?.row || 0,active.differences[0]?.column || 0)}>在表格编辑器调整</Button></div>
      <div className="structure-comparison"><div>{active.current_tables.map((t,i) => <TablePreview key={i} table={t} label="当前结构" />)}</div><div>{active.proposed_tables.map((t,i) => <TablePreview key={i} table={t} label="候选结构" />)}</div></div>
      <details className="structure-crop"><summary>展开局部原图</summary><RegionPreview version={version} location={location} tablePolygon={active.polygon} onReady={ready} /></details>
      <ul className="structure-differences">{active.differences.map((d,i) => <li key={i}>{d.row !== undefined ? `第 ${d.row+1} 行 ${d.column!+1} 列：` : ""}{reasons[d.kind] || d.kind}{typeof d.current === "number" ? ` ${d.current} → ${d.candidate}` : ""}</li>)}</ul>
      {!!active.conflicts.length && <div className="inline-warning"><strong>需先手工处理</strong><ul>{active.conflicts.map((c,i) => <li key={i}>{reasons[c.kind] || c.kind}{c.row !== undefined ? `（第 ${c.row+1} 行 ${c.column!+1} 列）` : ""}{c.text !== undefined ? `：${c.text || "人工清空"}` : ""}</li>)}</ul>
        {!!active.unassigned_tokens?.length && <p>未归属文字：{active.unassigned_tokens.map(t => t.raw_text).join("；")}</p>}</div>}
      {!!active.unverified_empty_cells?.length && <label className="structure-ack"><input type="checkbox" checked={acknowledged} disabled={disabled} onChange={e => setAcknowledged(e.target.checked)} />已查看 {active.unverified_empty_cells.length} 个无来源文字的空值，保留为待复核</label>}
      <details><summary>结构与文字来源</summary><p>结构：{active.provider}；文字结果：{active.text_source_result || "未确定"}</p><p>模型：{active.model_versions?.join("、") || "见原始候选"}</p><p>基准修订：{active.revision}；保留人工值：{active.retained_values?.filter(v => v.manual).length || 0}</p><code>{active.token_pool_sha256}</code></details>
      <div className="structure-actions"><Button appearance="primary" disabled={disabled || !active.can_apply || (!!active.unverified_empty_cells?.length && !acknowledged)} onClick={() => void decide("accept")}>接受结构建议</Button>
        <Button disabled={disabled || !["pending","deferred"].includes(active.state)} onClick={() => void decide("keep")}>保留当前结构</Button>
        <Button disabled={disabled || !["pending","deferred"].includes(active.state)} onClick={() => void decide("reject")}>拒绝此建议</Button>
        <Button disabled={disabled || !["pending","deferred"].includes(active.state)} onClick={() => void decide("defer")}>暂缓</Button></div>
      <p className="structure-source">接受后可使用编辑器「撤销」。结构变化后请重新检查候选并核对定位。</p>
    </> : <p className="structure-empty">当前没有待处理的结构建议。请继续核对漏表、文字和空值。</p>}
  </section>;
}
