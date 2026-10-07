import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, Dialog, DialogActions, DialogBody, DialogContent, DialogSurface, DialogTitle, Spinner } from "@fluentui/react-components";
import { Download, ScanLine, X } from "lucide-react";
import { api, request } from "./api";
import { RegionPreview } from "./RegionPreview";
import type { Photo, Result, Table, Version } from "./types";
import { screenCell, screenCellEdit, screenEditKey, screenIssueTitle, screenProgress, screenReviewBody, screenStatuses, selectedScreenVersions,
  type ScreenBatch, type ScreenCellEdit, type ScreenItem } from "./screenBatchWorkflow";
import "./screen-batches.css";

type PreparedImage = { id: string; kind: string; width: number; height: number; region_id?: string; path?: string };
type ScreenDetail = ScreenItem & {
  preparation?: { width: number; height: number; prepared_images: PreparedImage[]; warnings?: { message: string }[] };
  result?: Result | null;
  candidate_results?: { id: string; engine: string; raw: { tables: Table[] } }[];
};
type BatchListing = { batches: ScreenBatch[] } | ScreenBatch[];

const activeStates = new Set(["queued", "running", "prepared", "awaiting_upgrade"]);
const requestId = () => "screen-ui-" + crypto.randomUUID();

function PreparedPreview({ path, name }: { path: string; name: string }) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true, objectUrl = "";
    setUrl(""); setError("");
    request(path).then(response => response.blob()).then(blob => {
      objectUrl = URL.createObjectURL(blob);
      if (active) setUrl(objectUrl); else URL.revokeObjectURL(objectUrl);
    }).catch(e => { if (active) setError(String(e)); });
    return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [path]);
  return error ? <p role="alert">{error}</p> : url ? <img className="screen-prepared-image" src={url} alt={name} /> : <p>正在载入处理图…</p>;
}

export function ScreenBatchPanel({ projectId, images, selectedImageIds, disabled, onError, onRefresh, onOpenResult }: {
  projectId: string; images: Photo[]; selectedImageIds: string[]; disabled?: boolean;
  onError: (message: string) => void; onRefresh: () => Promise<unknown>;
  onOpenResult: (resultId: string, imageId: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [scope, setScope] = useState<"selected" | "project">("project");
  const [precision, setPrecision] = useState(false);
  const [batches, setBatches] = useState<ScreenBatch[]>([]);
  const [batchId, setBatchId] = useState("");
  const [batch, setBatch] = useState<ScreenBatch | null>(null);
  const [filter, setFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [detailId, setDetailId] = useState("");
  const [detail, setDetail] = useState<ScreenDetail | null>(null);
  const [issueIndex, setIssueIndex] = useState(0);
  const [tableIndex, setTableIndex] = useState(0);
  const [row, setRow] = useState(0), [column, setColumn] = useState(0);
  const [edits, setEdits] = useState<Record<string, ScreenCellEdit>>({});
  const [previewId, setPreviewId] = useState("original");
  const [includeReview, setIncludeReview] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const projectRef = useRef(projectId);
  const submission = useRef<{ requestId: string; signature: string } | null>(null);
  const generation = useRef(0);
  projectRef.current = projectId;
  const base = `/projects/${encodeURIComponent(projectId)}/screen-batches`;
  const versions = useMemo(() => selectedScreenVersions(images, selectedImageIds, scope), [images, selectedImageIds, scope]);

  useEffect(() => {
    generation.current++;
    setBatchId(""); setBatch(null); setBatches([]); setDetailId(""); setDetail(null); setEdits({});
    setError(""); setNotice(""); setFilter(""); setOffset(0); setBusy(false); submission.current = null;
  }, [projectId]);

  const report = useCallback((value: unknown) => {
    const message = String(value).replace(/^Error: /, "");
    setError(message); onError(message);
  }, [onError]);

  const listBatches = useCallback(async () => {
    const project = projectId;
    const value = await api<BatchListing>(base);
    if (projectRef.current !== project) return;
    const available = Array.isArray(value) ? value : value.batches;
    setBatches(available || []);
    setBatchId(current => current || available?.[0]?.batch_id || "");
  }, [base, projectId]);

  useEffect(() => {
    if (!open || !projectId) return;
    void listBatches().catch(report);
  }, [open, projectId, listBatches, report]);

  useEffect(() => {
    if (!open || !batchId || !projectId) return;
    let active = true, pending = false;
    const load = async () => {
      if (pending) return;
      pending = true;
      try {
        const value = await api<ScreenBatch>(`${base}/${encodeURIComponent(batchId)}?offset=${offset}&limit=30${filter ? "&status=" + filter : ""}`);
        if (active) { setBatch(value); setError(""); }
      } catch (e) { if (active) report(e); }
      finally { pending = false; }
    };
    setBatch(null);
    void load();
    const timer = setInterval(() => { if (!document.hidden) void load(); }, 2500);
    return () => { active = false; clearInterval(timer); };
  }, [open, projectId, batchId, base, offset, filter, report]);

  const loadDetail = useCallback(async (versionId: string) => {
    const project = projectId, batchValue = batchId;
    const value = await api<ScreenDetail>(`${base}/${encodeURIComponent(batchValue)}/items/${encodeURIComponent(versionId)}`);
    if (projectRef.current !== project) return null;
    return value;
  }, [projectId, batchId, base]);

  useEffect(() => {
    if (!open || !detailId || !batchId) { setDetail(null); return; }
    let active = true;
    setDetail(null); setEdits({}); setIssueIndex(0); setPreviewId("original");
    void loadDetail(detailId).then(value => { if (active && value) { setDetail(value); setTableIndex(value.issues[0]?.table || 0); setRow(value.issues[0]?.row || 0); setColumn(value.issues[0]?.column || 0); } }).catch(e => { if (active) report(e); });
    return () => { active = false; };
  }, [open, detailId, batchId, loadDetail, report]);

  const issue = detail?.issues[issueIndex];
  const tables = detail?.result?.edited.tables || [];
  const cell = screenCell(tables[tableIndex], row, column);
  const editKey = cell ? screenEditKey({ table_index: tableIndex, row: cell.row, column: cell.column }) : "";
  const cellValue = cell ? edits[editKey]?.after ?? cell.text : "";
  const chooseIssue = (index: number) => {
    setIssueIndex(index);
    const selected = detail?.issues[index];
    if (Number.isInteger(selected?.table)) setTableIndex(selected!.table!);
    if (Number.isInteger(selected?.row)) setRow(selected!.row!);
    if (Number.isInteger(selected?.column)) setColumn(selected!.column!);
    setPreviewId("original");
  };
  const editValue = (after: string) => {
    if (!cell) return;
    setEdits(current => ({ ...current, [editKey]: screenCellEdit(tableIndex, cell, after) }));
  };
  const editedCount = Object.values(edits).filter(edit => edit.after !== edit.before).length;

  const run = async (operation: () => Promise<void>) => {
    const owner = generation.current;
    setBusy(true); setError(""); setNotice("");
    try { await operation(); return true; }
    catch (e) { if (owner === generation.current) report(e); return false; }
    finally { if (owner === generation.current) setBusy(false); }
  };
  const startBatch = () => run(async () => {
    if (!versions.length || versions.length > 1000) throw Error("请选择 1–1000 张图片后开始。");
    const signature = JSON.stringify({ projectId, versions, precision });
    if (submission.current?.signature !== signature) submission.current = { signature, requestId: requestId() };
    const value = await api<ScreenBatch>(base, "POST", { version_ids: versions, request_id: submission.current.requestId,
      policy: { primary_engine: "auto", mode: precision ? "accuracy" : "auto" } });
    if (projectRef.current !== projectId) return;
    setBatchId(value.batch_id); setBatch(value); setFilter(""); setOffset(0); setDetailId("");
    submission.current = null;
    await listBatches(); await onRefresh();
    setNotice(`已开始处理 ${versions.length} 张图片；每图一个 Excel 文件。`);
  });
  const batchAction = (action: string, extra: Record<string, unknown> = {}) => run(async () => {
    const value = await api<ScreenBatch>(`${base}/${encodeURIComponent(batchId)}/action`, "POST", { action, request_id: requestId(), ...extra });
    if (projectRef.current !== projectId) return;
    setBatch(value);
    if (value.batch_id !== batchId) {
      setBatchId(value.batch_id); setDetailId(""); setOffset(0); setFilter("");
      await listBatches();
    } else if (detailId) { const current = await loadDetail(detailId); if (current) setDetail(current); }
    await onRefresh();
  });
  const review = (state: "accepted" | "retain" | "deferred") => {
    if (!detail?.result) return;
    const body = screenReviewBody(detail, detail.result.revision, state, Object.values(edits), requestId());
    void batchAction("review", body).then(success => { if (success) setEdits({}); });
  };
  const exportBatch = () => run(async () => {
    const response = await request(`${base}/${encodeURIComponent(batchId)}/export`, { method: "POST", body: JSON.stringify({ include_pending_review: includeReview }) });
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = "screen-tables.zip"; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1500);
    setNotice("已下载逐图 Excel 与输入/结果清单。");
  });
  const progress = batch ? screenProgress(batch.counts) : null;
  const active = batch && (activeStates.has(batch.status) || batch.counts.pending > 0 && batch.status !== "paused");
  const version: Version | null = detail?.preparation ? { id: detail.input_version_id, image_id: detail.image_id,
    width: detail.preparation.width, height: detail.preparation.height, parent_id: null, operations: "[]", created: "" } : null;
  const localized = cell?.polygon || issue?.polygon || (issue?.box?.length === 4 ? [[issue.box[0], issue.box[1]], [issue.box[2], issue.box[1]], [issue.box[2], issue.box[3]], [issue.box[0], issue.box[3]]] : null);
  const close = () => { if (busy) return; if (editedCount) { setError("请先保存单元格修改，或清除待保存修改后关闭。"); return; } setOpen(false); };

  return <>
    <Button icon={<ScanLine size={16} />} disabled={!projectId} onClick={() => { setScope(selectedImageIds.length ? "selected" : "project"); setOpen(true); }}>拍屏表格批量转 Excel</Button>
    <Dialog open={open} onOpenChange={(_, data) => { if (!data.open) close(); }}>
      <DialogSurface className="screen-batch-dialog">
        <DialogBody>
          <DialogTitle action={<Button appearance="subtle" aria-label="关闭批量表格处理" icon={<X size={18} />} disabled={busy} onClick={close} />}>拍屏表格批量转 Excel</DialogTitle>
          <DialogContent className="screen-batch-content">
            <p className="screen-batch-intro">自动处理业务表区，每张图片生成一个 Excel；同图多表放入不同工作表。疑难项在这里集中核对。</p>
            {error && <div className="inline-warning" role="alert">{error}</div>}
            {notice && <p role="status">{notice}</p>}
            <div className="screen-batch-start">
              <label>处理范围<select aria-label="批量表格处理范围" value={scope} onChange={e => setScope(e.target.value as "selected" | "project")}>
                <option value="project">本项目全部图片（{images.length} 张）</option>
                <option value="selected" disabled={!selectedImageIds.length}>已选图片（{selectedImageIds.length} 张）</option>
              </select></label>
              <label className="screen-batch-check"><input type="checkbox" checked={precision} onChange={e => setPrecision(e.target.checked)} />提高精度（增加必要核对）</label>
              <Button appearance="primary" disabled={busy || disabled || !!editedCount || !versions.length || versions.length > 1000} onClick={() => void startBatch()}>开始处理 {versions.length} 张</Button>
              {disabled && <span>当前识别不可用，可继续核对和下载已有批次。</span>}
            </div>
            <div className="screen-batch-pick">
              <label>最近批次<select aria-label="最近表格批次" value={batchId} disabled={!!editedCount} onChange={e => { setBatchId(e.target.value); setDetailId(""); setOffset(0); setFilter(""); }}>
                <option value="">选择已有批次</option>{batches.map(value => <option value={value.batch_id} key={value.batch_id}>{value.created?.slice(0,16).replace("T"," ")} · {value.counts?.total || ""} 张 · {screenStatuses[value.status] || value.status}</option>)}
              </select></label>
              <Button appearance="subtle" disabled={busy} onClick={() => void listBatches().catch(report)}>刷新批次</Button>
            </div>
            {batch ? <>
              <div className="screen-batch-progress" aria-live="polite">
                <div><strong>{screenStatuses[batch.status] || batch.status}</strong><span>{progress?.terminal} / {batch.counts.total} 张已处理</span></div>
                <progress max={100} value={progress?.percent} aria-label="批次处理进度" />
                <dl>{[["可交付",batch.counts.completed],["处理中",batch.counts.pending],["待核对",batch.counts.needs_review],["失败",batch.counts.failed],["已取消",batch.counts.cancelled]].map(([label,count]) => <div key={label}><dt>{label}</dt><dd>{count}</dd></div>)}</dl>
              </div>
              {batch.worker_error && <p className="inline-warning" role="alert">处理服务正在恢复：{batch.worker_error}</p>}
              <div className="screen-batch-actions">
                {active && <Button disabled={busy} onClick={() => void batchAction("pause")}>暂停批次</Button>}
                {batch.status === "paused" && <Button disabled={busy || disabled} onClick={() => void batchAction("resume")}>继续处理</Button>}
                {batch.counts.pending > 0 && <Button disabled={busy} onClick={() => void batchAction("cancel")}>停止未完成项</Button>}
                <Button disabled={busy || disabled || !batch.counts.failed} onClick={() => void batchAction("retry_failed")}>只重试失败项（{batch.counts.failed}）</Button>
                <label className="screen-batch-check"><input type="checkbox" checked={includeReview} onChange={e => setIncludeReview(e.target.checked)} />下载时包含待核对结果</label>
                <Button appearance="primary" icon={<Download size={15} />} disabled={busy || !(batch.counts.completed || includeReview && batch.counts.needs_review)} onClick={() => void exportBatch()}>下载逐图 Excel ZIP</Button>
              </div>
              <p className="screen-batch-small">默认按原文保存金额和编号，保留前导零及长编号。自动交付与人工核对分别记录；未完成和失败项列在下载清单中。</p>
              <div className="screen-batch-workspace">
                <section className="screen-batch-items" aria-label="批次图片列表">
                  <label>筛选<select aria-label="批次图片状态筛选" value={filter} onChange={e => { setFilter(e.target.value); setOffset(0); }}>
                    <option value="">全部</option><option value="needs_review">待核对</option><option value="failed">失败</option><option value="completed">可交付</option><option value="pending">处理中</option>
                  </select></label>
                  <div className="screen-item-list">{batch.items.map(item => <button key={item.input_version_id} className={"screen-item " + (detailId === item.input_version_id ? "selected" : "")} onClick={() => { if (editedCount) { setError("请先保存或清除当前单元格修改，再切换图片。"); return; } setDetailId(item.input_version_id); }}>
                    <strong>{item.source_name}</strong><span>{screenStatuses[item.status] || item.status}{item.human_confirmed ? " · 人工已核对" : item.status === "completed" ? " · 自动交付" : ""}</span>
                    {item.issues.length > 0 && <small>{item.issues.length} 项提示</small>}{item.error && <small className="screen-item-error">{item.error}</small>}
                  </button>)}</div>
                  {!batch.items.length && <p>当前筛选下没有图片。</p>}
                  <div className="screen-batch-pages"><Button size="small" disabled={!offset} onClick={() => setOffset(Math.max(0,offset-30))}>上一页</Button><span>{batch.total_items ? offset+1 : 0}–{Math.min(offset+30,batch.total_items)} / {batch.total_items}</span><Button size="small" disabled={batch.next_offset === null} onClick={() => setOffset(batch.next_offset || 0)}>下一页</Button></div>
                </section>
                <section className="screen-batch-review" aria-label="单图集中核对">
                  {!detailId ? <p>选择一张图片查看业务表区、识别结果和疑点。</p> : !detail ? <Spinner label="正在载入图片详情" /> : <>
                    <h3>{detail.source_name}</h3>
                    <div className="screen-batch-detail-actions"><span>{screenStatuses[detail.status] || detail.status}</span>
                      {detail.result_id && <Button size="small" disabled={!!editedCount} onClick={() => { setOpen(false); void onOpenResult(detail.result_id!,detail.image_id).catch(report); }}>打开完整表格编辑器</Button>}
                      {detail.status === "failed" && <Button size="small" disabled={busy || disabled} onClick={() => void batchAction("retry_failed",{version_ids:[detail.input_version_id]})}>重试这张图</Button>}
                      {detail.result_id && <Button size="small" disabled={busy || disabled || !!editedCount || activeStates.has(detail.status)} onClick={() => void batchAction("upgrade",{version_ids:[detail.input_version_id]})}>只对这张图提高精度</Button>}
                    </div>
                    {detail.error && <p role="alert">{detail.error}</p>}
                    {detail.issues.length > 0 && <label>需要核对<select aria-label="本图疑点" value={issueIndex} onChange={e => chooseIssue(Number(e.target.value))}>{detail.issues.map((item,index) => <option key={index} value={index}>{index+1}. {screenIssueTitle(item)}</option>)}</select></label>}
                    {issue && <div className={"screen-issue " + (issue.severity === "high" ? "high" : "")}>
                      <strong>{screenIssueTitle(issue)}</strong>
                      {issue.value !== undefined && <p>当前值：<code>{String(issue.value)}</code></p>}
                      {issue.alternative !== undefined && <p>另一转录：<code>{String(issue.alternative)}</code></p>}
                      {issue.visible_text !== undefined && <p>文字证据：<code>{String(issue.visible_text)}</code></p>}
                      <p>请结合原图确认；模型一致或合计一致不能替代核对。</p>
                    </div>}
                    {detail.preparation && <>
                      <label>原图与处理图<select aria-label="核对图片范围" value={previewId} onChange={e => setPreviewId(e.target.value)}><option value="original">原图（支持局部核对）</option>{detail.preparation.prepared_images.filter(p => p.id !== "original").map(p => <option key={p.id} value={p.id}>{p.kind === "table_crop" ? "业务表区" : p.kind === "perspective" ? "透视校正候选" : "局部分块"} {p.region_id || ""}</option>)}</select></label>
                      {previewId === "original" && version ? <RegionPreview version={version} location={{level:localized ? "cell" : "image",polygon:localized,version_id:version.id,reason:localized ? "原图局部定位" : "无可靠单格定位，显示全图"}} />
                        : <PreparedPreview path={`${base}/${encodeURIComponent(batchId)}/items/${encodeURIComponent(detail.input_version_id)}/files/${encodeURIComponent(previewId)}`} name="业务处理图" />}
                    </>}
                    {detail.result && <>
                      <div className="screen-cell-position"><label>表<select aria-label="核对表格" value={tableIndex} onChange={e => { setTableIndex(Number(e.target.value)); setRow(0); setColumn(0); }}>{tables.map((_,index) => <option value={index} key={index}>表 {index+1}</option>)}</select></label>
                        <label>行<input aria-label="核对行" type="number" min={1} max={tables[tableIndex]?.rows || 1} value={row+1} onChange={e => setRow(Math.max(0,Number(e.target.value)-1))} /></label>
                        <label>列<input aria-label="核对列" type="number" min={1} max={tables[tableIndex]?.columns || 1} value={column+1} onChange={e => setColumn(Math.max(0,Number(e.target.value)-1))} /></label></div>
                      {cell ? <label>当前单元格<textarea aria-label="当前单元格内容" value={cellValue} onChange={e => editValue(e.target.value)} rows={2} /></label> : <p>该格位没有独立单元格，请在完整表格编辑器核对结构。</p>}
                      {cell && issue?.alternative !== undefined && Number.isInteger(issue.table) && issue.table === tableIndex && issue.row === cell.row && issue.column === cell.column && <Button size="small" disabled={busy} onClick={() => editValue(String(issue.alternative))}>填入另一转录，等待保存核对</Button>}
                      <p className="screen-batch-small">待保存修改：{editedCount} 格 · 保存前按结果版本检查，防止覆盖新编辑。</p>
                      {editedCount > 0 && <Button size="small" disabled={busy} onClick={() => setEdits({})}>清除待保存修改</Button>}
                      <div className="screen-review-actions"><Button appearance="primary" disabled={busy || !detail.result_id || editedCount > 100 || activeStates.has(detail.status)} onClick={() => review("accepted")}>保存并标记核对完成</Button>
                        <Button disabled={busy || editedCount > 100 || activeStates.has(detail.status)} onClick={() => review("retain")}>保留当前内容，仍待核对</Button><Button disabled={busy || editedCount > 0 || activeStates.has(detail.status)} onClick={() => review("deferred")}>稍后核对</Button></div>
                    </>}
                  </>}
                </section>
              </div>
            </> : batchId ? <Spinner label="正在载入批次" /> : <p className="screen-batch-empty">选择处理范围后开始。200 张以上可一次发起，关闭此面板后后台继续运行。</p>}
          </DialogContent>
          <DialogActions><Button disabled={busy} onClick={close}>关闭面板，后台继续</Button></DialogActions>
        </DialogBody>
      </DialogSurface>
    </Dialog>
  </>;
}
