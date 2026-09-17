import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Select, Spinner } from "@fluentui/react-components";
import { Download, LocateFixed, RefreshCw, ScanLine } from "lucide-react";
import { api, request } from "./api";
import type { GeometryTarget } from "./GeometryPanel";
import { RegionPreview, type Location } from "./RegionPreview";
import type { Result, Version } from "./types";
import { usePreference } from "./workspacePreferences";
import {
  multimodalTargetLabel, readMultimodalPending, writeMultimodalPending,
  type MultimodalCatalog, type MultimodalDecision, type MultimodalProposal,
  type MultimodalRequest, type MultimodalView, type PendingMultimodalRequest,
} from "./multimodalTypes";
import "./multimodal.css";

type Props = {
  result: Result;
  version: Version;
  busy: boolean;
  adopted: boolean;
  target?: GeometryTarget | null;
  refreshKey: number;
  focusId?: string;
  beforeSubmit: () => Promise<Result>;
  onResult: (result: Result) => Promise<void> | void;
  onDecision?: (proposalId: string, body: MultimodalDecision) => Promise<Result>;
  getPendingDecision?: () => { resultId?: string; issueId: string; body: Record<string, unknown> } | null;
  onLocate: (location: Location) => void;
  onQueued: () => Promise<void> | void;
};
const activeStatuses = new Set(["queued", "running", "paused", "interrupted"]);
const taskNames: Record<string, string> = {
  queued: "等待审校", running: "审校中", succeeded: "审校完成", failed: "审校失败",
  cancelled: "已取消", paused: "已暂停", interrupted: "等待恢复",
};
const decisionNames = { keep: "建议保留", replace: "建议修改", uncertain: "模型存疑" };
const stateNames = { pending: "待人工处理", accepted: "已采用", rejected: "已拒绝", question: "人工存疑", stale: "建议已过期" };
const contextKey = (resultId: string, versionId: string) => `${resultId}:${versionId}`;

export function MultimodalReview({ result, version, busy, adopted, target, refreshKey, focusId, beforeSubmit, onResult, onDecision, getPendingDecision, onLocate, onQueued }: Props) {
  const context = contextKey(result.id, version.id);
  const latestContext = useRef(context);
  latestContext.current = context;
  const [catalog, setCatalog] = useState<MultimodalCatalog | null>(null);
  const [catalogError, setCatalogError] = useState("");
  const [modelId, setModelId] = usePreference("ocr-multimodal-model", "", (value): value is string => typeof value === "string" && value.length <= 200);
  const [snapshot, setSnapshot] = useState<{ context: string; value: MultimodalView } | null>(null);
  const [selected, setSelected] = useState("");
  const [filter, setFilter] = useState("pending");
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState<PendingMultimodalRequest | null>(() => readMultimodalPending(result.id, version.id));
  const [reportFormat, setReportFormat] = useState("xlsx");
  const actionLock = useRef(false);
  const loadSequence = useRef(0);
  const controllers = useRef(new Set<AbortController>());
  const mounted = useRef(true);
  const view = snapshot?.context === context ? snapshot.value : null;
  const editorPending = getPendingDecision?.();
  const managedDecision = !!onDecision && !!getPendingDecision;
  const editorRecovery: PendingMultimodalRequest | null = editorPending?.body.decision_kind === "multimodal" &&
    (!editorPending.resultId || editorPending.resultId === result.id) && editorPending.body.version_id === version.id
    ? { kind: "decision", result_id: result.id, version_id: version.id, proposal_id: editorPending.issueId,
      body: { action: editorPending.body.action as MultimodalDecision["action"], request_id: editorPending.body.request_id as string,
        revision: editorPending.body.revision as number, version_id: version.id } } : null;
  const recovery = editorRecovery || (pending?.result_id === result.id && pending.version_id === version.id &&
    !(managedDecision && pending.kind === "decision") ? pending : null);
  const model = catalog?.models.find(item => item.id === modelId);
  const proposals = (view?.proposals || []).filter(item =>
    filter === "all" || (filter === "pending" ? item.state === "pending" || item.state === "question" : item.decision === filter));
  const active = proposals.find(item => item.id === selected) || proposals[0];
  const focusExists = !!focusId && !!view?.proposals.some(item => item.id === focusId);
  const running = view?.requests.some(item => activeStatuses.has(item.status));
  const disabled = busy || working || !!recovery || !!editorPending;
  const canDecide = adopted && !disabled && active && ["pending", "question"].includes(active.state);
  const isCurrent = useCallback((key: string) => mounted.current && latestContext.current === key, []);

  const load = useCallback(async (signal?: AbortSignal) => {
    const sequence = ++loadSequence.current;
    const value = await (await request(`/results/${result.id}/multimodal`, { signal })).json() as MultimodalView;
    if (isCurrent(context) && sequence === loadSequence.current) setSnapshot({ context, value });
  }, [result.id, context, isCurrent]);

  const loadModels = useCallback(async (signal?: AbortSignal) => {
    try {
      const value = await (await request("/multimodal/models", { signal })).json() as MultimodalCatalog;
      if (!mounted.current || signal?.aborted) return;
      setCatalog(value); setCatalogError("");
      setModelId(current => value.models.some(item => item.id === current) ? current :
        value.models.find(item => item.id === value.default_model)?.id || value.models.find(item => item.available)?.id || value.models[0]?.id || "");
    } catch (e) { if (!signal?.aborted && mounted.current) setCatalogError(String(e)); }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void loadModels(controller.signal);
    return () => { mounted.current = false; controller.abort(); controllers.current.forEach(item => item.abort()); };
  }, [loadModels]);

  useEffect(() => {
    setSelected(""); setFilter("pending"); setError(""); setMessage("");
    setPending(readMultimodalPending(result.id, version.id));
    controllers.current.forEach(controller => controller.abort());
    controllers.current.clear();
  }, [context, result.id, version.id]);

  useEffect(() => { if (focusId && focusExists) { setSelected(focusId); setFilter("all"); } }, [focusId, focusExists, context]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal).catch(e => { if (!controller.signal.aborted && isCurrent(context)) setError(String(e)); });
    return () => { controller.abort(); };
  }, [load, result.revision, refreshKey, context, isCurrent]);

  useEffect(() => {
    if (!running) return;
    let ended = false;
    let timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const poll = async () => {
      try { await load(controller.signal); }
      catch (e) { if (!ended && isCurrent(context)) setError(String(e)); }
      if (!ended) timer = setTimeout(poll, 1800);
    };
    timer = setTimeout(poll, 1800);
    return () => { ended = true; clearTimeout(timer); controller.abort(); };
  }, [running, context, load, isCurrent]);

  const remember = (value: PendingMultimodalRequest | null, forResult = result.id, forVersion = version.id) => {
    if (managedDecision && value?.kind === "decision") return;
    writeMultimodalPending(forResult, forVersion, value);
    if (isCurrent(contextKey(forResult, forVersion))) setPending(value);
  };
  const run = async (work: () => Promise<void>) => {
    if (actionLock.current) return;
    actionLock.current = true; setWorking(true); setError(""); setMessage("");
    try { await work(); }
    catch (e) { if (isCurrent(context)) setError(String(e)); }
    finally { actionLock.current = false; if (mounted.current) setWorking(false); }
  };

  const sendPending = async (value: PendingMultimodalRequest) => {
    const controller = new AbortController();
    controllers.current.add(controller);
    const path = `/results/${value.result_id}/multimodal${value.kind === "decision" ? `/${value.proposal_id}/decision` : ""}`;
    try {
      const response = value.kind === "decision" && onDecision
        ? await onDecision(value.proposal_id, value.body)
        : await (await request(path, { method: "POST", body: JSON.stringify(value.body), signal: controller.signal })).json();
      if (!isCurrent(context)) return;
      if (value.kind === "decision") {
        const next = response.result || (response.id ? response : await api<Result>(`/results/${value.result_id}`));
        if (!isCurrent(context)) return;
        await onResult(next as Result);
        if (!isCurrent(context)) return;
        setMessage(value.body.action === "accept" ? "已记录这条建议的人工处理；内容修改可在编辑器撤销。" : value.body.action === "reject" ? "已拒绝这条建议，识别内容未修改。" : "已标记为人工存疑，识别内容未修改。");
      } else setMessage("审校任务已加入队列。完成后请逐条对照原图处理建议。");
      remember(null, value.result_id, value.version_id);
      await load();
      if (isCurrent(context)) await onQueued();
    } finally { controllers.current.delete(controller); }
  };

  const submit = (scope: "page" | "target") => run(async () => {
    if (recovery || !model?.available || !adopted || (scope === "target" && !target)) return;
    const current = await beforeSubmit();
    if (!isCurrent(context) || current.id !== result.id) return;
    const value: PendingMultimodalRequest = { kind: "submit", result_id: current.id, version_id: version.id,
      body: { revision: current.revision, version_id: version.id, model_id: modelId, scope,
        ...(scope === "target" && target ? { target } : {}), request_id: crypto.randomUUID() } };
    remember(value);
    await sendPending(value);
  });

  const decide = (action: MultimodalDecision["action"]) => run(async () => {
    if (!active || !canDecide || (action === "accept" && active.decision === "uncertain")) return;
    const current = await beforeSubmit();
    if (!isCurrent(context) || current.id !== result.id) return;
    const value: PendingMultimodalRequest = { kind: "decision", result_id: current.id, version_id: version.id, proposal_id: active.id,
      body: { action, revision: current.revision, version_id: version.id, request_id: crypto.randomUUID() } };
    remember(value);
    await sendPending(value);
  });

  const taskAction = (task: MultimodalRequest, action: "cancel" | "retry" | "resume") => run(async () => {
    await api(`/results/${result.id}/multimodal/tasks/${task.task_id}/${action}`, "POST", {});
    if (!isCurrent(context)) return;
    await load();
    if (isCurrent(context)) await onQueued();
  });

  const exportReport = () => run(async () => {
    const current = await beforeSubmit();
    if (!isCurrent(context) || current.id !== result.id) return;
    const response = await request(`/results/${result.id}/multimodal/report?format=${reportFormat}`);
    const blob = await response.blob();
    if (!isCurrent(context)) return;
    const url = URL.createObjectURL(blob), anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = response.headers.get("Content-Disposition")?.match(/filename="([^"]+)"/)?.[1] || `OCR-review-${result.id}.${reportFormat}`;
    anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    setMessage("校验清单已导出，包含模型建议与人工处理状态。");
  });

  return <section className="multimodal-review" aria-label="多模态二轮审校" aria-busy={working}>
    <div className="multimodal-heading"><strong><ScanLine size={17} />多模态二轮审校</strong><span>原图复核 · 人工采用</span></div>
    <p className="multimodal-intro">结合原图审查现有文字和表格内容，提出保留、修改或存疑建议。模型建议需要人工核对。</p>
    <div className="multimodal-model-row">
      <label htmlFor="multimodal-model">审校模型</label>
      <Select id="multimodal-model" size="small" value={modelId} disabled={!catalog || disabled} onChange={(_, data) => setModelId(data.value)}>
        {!catalog?.models.length && <option value="">{catalog ? "暂无可用模型" : "正在读取模型…"}</option>}
        {catalog?.models.map(item => <option key={item.id} value={item.id} disabled={!item.available}>{item.label}{!item.available ? "（不可用）" : ""}</option>)}
      </Select>
      <Button size="small" appearance="subtle" icon={<RefreshCw size={14} />} disabled={working} onClick={() => void loadModels()}>刷新模型</Button>
    </div>
    {catalogError && <p className="inline-warning" role="alert">模型列表读取失败：{catalogError}</p>}
    {model && !model.available && <p className="multimodal-warning">{model.reason || "此模型当前不可用，请先完成模型服务配置。"}</p>}
    {catalog && !catalog.models.length && <p className="multimodal-warning">{catalog.reason || "尚未配置审校模型。配置兼容的视觉模型服务后，点击「刷新模型」。"}</p>}
    {!!catalog?.models.some(item => !item.available && item.id !== modelId) && <details className="multimodal-model-reasons"><summary>查看不可用模型</summary><ul>{catalog.models.filter(item => !item.available && item.id !== modelId).map(item => <li key={item.id}>{item.label}：{item.reason || "模型服务未就绪"}</li>)}</ul></details>}
    <div className="multimodal-actions">
      <Button appearance="primary" size="small" disabled={disabled || !adopted || !model?.available || !target} onClick={() => void submit("target")}>复核选中内容</Button>
      <Button size="small" disabled={disabled || !adopted || !model?.available} onClick={() => void submit("page")}>复核本页</Button>
    </div>
    <p className="multimodal-selection">{target ? `当前选中：${multimodalTargetLabel(target)}` : "局部复核：在文字编辑区选中一段文字，或选中表格单元格。"}</p>
    {!adopted && <p className="multimodal-warning">请先采用此识别结果，再发起审校或处理建议。</p>}
    {recovery && <div className="multimodal-warning" role="status"><p>上次{recovery.kind === "decision" ? "人工处理" : "审校提交"}请求尚未确认。重试会沿用原请求，避免重复提交。</p>
      <div className="multimodal-actions"><Button size="small" disabled={busy || working} onClick={() => void run(() => sendPending(recovery))}>重试上次请求</Button>
        {!(managedDecision && recovery.kind === "decision") && <Button size="small" disabled={busy || working} onClick={() => { remember(null); setMessage("已结束这次请求的重试。请先核对任务和建议状态，再发起新操作。"); void load().catch(e => { if (isCurrent(context)) setError(String(e)); }); }}>结束重试</Button>}</div>
        {managedDecision && recovery.kind === "decision" && <p>如需放弃待核实决策，请使用编辑器的「放弃本次修改并重新加载」。</p>}</div>}
    {error && <p className="inline-warning" role="alert">{error}</p>}
    {message && <p className="multimodal-message" role="status">{message}</p>}
    {!view && !error && <div className="multimodal-loading"><Spinner size="tiny" />正在读取审校记录…</div>}
    {!!view?.requests.length && <details className="multimodal-tasks" open={!!running}>
      <summary>审校任务 · {view.requests.length} 次{running ? " · 处理中" : ""}</summary>
      <ul>{view.requests.slice().sort((a, b) => b.created.localeCompare(a.created)).map(task => <li key={task.task_id}>
        <div><strong>{taskNames[task.status] || task.status}</strong><span>{task.scope === "page" ? "本页" : "选中内容"} · {catalog?.models.find(item => item.id === task.model_id)?.label || task.model_id}</span></div>
        <p>{task.phase || "等待状态更新"}</p>{task.error && <p className="multimodal-warning">{task.error}</p>}
        {task.snapshot_current === false && <p>本次任务依据的内容已变化。需要再次审校时，请按当前内容重新发起。</p>}
        <div className="multimodal-actions">{["queued", "running"].includes(task.status) && <Button size="small" disabled={disabled} onClick={() => void taskAction(task, "cancel")}>取消审校</Button>}
          {["failed", "cancelled"].includes(task.status) && <Button size="small" disabled={disabled || !adopted || task.snapshot_current === false} onClick={() => void taskAction(task, "retry")}>重试任务</Button>}
          {["paused", "interrupted"].includes(task.status) && <><Button size="small" disabled={disabled || !adopted || task.snapshot_current === false} onClick={() => void taskAction(task, "resume")}>恢复任务</Button><Button size="small" disabled={disabled} onClick={() => void taskAction(task, "cancel")}>取消审校</Button></>}
        </div>
      </li>)}</ul>
    </details>}
    {view && <div className="multimodal-proposals">
      <div className="multimodal-summary"><strong>审校建议</strong><span>待处理 {view.counts.pending || 0} · 人工存疑 {view.counts.question || 0} · 已采用 {view.counts.accepted || 0}</span></div>
      {!!view.proposals.length && <div className="multimodal-filter"><label htmlFor="multimodal-filter">显示</label><Select id="multimodal-filter" size="small" value={filter} disabled={working} onChange={(_, data) => { setFilter(data.value); setSelected(""); }}>
        <option value="pending">待处理与人工存疑</option><option value="replace">模型建议修改</option><option value="uncertain">模型存疑</option><option value="keep">模型建议保留</option><option value="all">全部建议（含已处理）</option>
      </Select></div>}
      {active ? <>
        <Select aria-label="选择审校建议" className="multimodal-proposal-select" value={active.id} disabled={working} onChange={(_, data) => setSelected(data.value)}>
          {proposals.map(item => <option key={item.id} value={item.id}>{multimodalTargetLabel(item.target)} · {decisionNames[item.decision]} · {stateNames[item.state]}</option>)}
        </Select>
        <ProposalContent proposal={active} version={version} onLocate={onLocate} />
        <div className="multimodal-actions">
          {active.decision === "replace" && <Button size="small" appearance="primary" disabled={!canDecide} onClick={() => void decide("accept")}>采用这条修改</Button>}
          {active.decision === "keep" && <Button size="small" disabled={!canDecide} onClick={() => void decide("accept")}>记录保留原文</Button>}
          <Button size="small" disabled={!canDecide} onClick={() => void decide("reject")}>拒绝此建议</Button>
          <Button size="small" disabled={!canDecide || active.state === "question"} onClick={() => void decide("question")}>人工标记存疑</Button>
        </div>
        <p className="multimodal-footnote">采用修改会记录修订历史，可撤销。模型建议不等同于整页人工确认。</p>
      </> : <p className="multimodal-empty">{view.proposals.length ? "当前筛选下没有建议。可切换到「全部建议」查看已处理记录。" : running ? "审校正在处理，建议完成后会显示在这里。" : view.requests.some(task => task.status === "succeeded") ? "本轮未返回内容建议，仍需人工检查遗漏和识别内容。" : "尚无审校建议。选定模型后，可复核选中内容或本页。"}</p>}
    </div>}
    <div className="multimodal-export"><label htmlFor="multimodal-report">校验清单</label><Select id="multimodal-report" size="small" value={reportFormat} disabled={disabled} onChange={(_, data) => setReportFormat(data.value)}><option value="xlsx">Excel</option><option value="md">Markdown</option><option value="json">JSON</option></Select>
      <Button size="small" icon={<Download size={14} />} disabled={disabled || !view} onClick={() => void exportReport()}>导出校验清单</Button>
    </div>
  </section>;
}

function ProposalContent({ proposal, version, onLocate }: { proposal: MultimodalProposal; version: Version; onLocate: (location: Location) => void }) {
  const evidence = proposal.evidence || { level: "image", polygon: null, version_id: proposal.version_id || version.id, reason: "没有可靠局部定位，显示全图" };
  return <div className="multimodal-proposal">
    <div className="multimodal-proposal-heading"><span className={`multimodal-badge ${proposal.decision}`}>{decisionNames[proposal.decision]}</span><span>{stateNames[proposal.state]}</span></div>
    {proposal.state === "stale" && <p className="multimodal-warning">内容或图片已变化，这条建议无法采用。请根据当前内容重新发起审校。</p>}
    <div className="multimodal-comparison"><div><strong>审校时的原文</strong><pre>{proposal.before || "（空）"}</pre></div><div><strong>{proposal.decision === "replace" ? "模型建议文字" : "模型意见"}</strong><pre>{proposal.decision === "replace" ? proposal.after ?? "（无建议文字）" : proposal.decision === "keep" ? "建议保留原文，仍需人工核对" : "无法确定，请对照原图核对"}</pre></div></div>
    <p className="multimodal-reason"><strong>模型依据</strong>{proposal.reason || "模型未提供具体依据"}</p>
    <div className="multimodal-actions"><Button size="small" icon={<LocateFixed size={14} />} disabled={evidence.version_id !== version.id} onClick={() => onLocate(evidence)}>定位原图</Button></div>
    {evidence.version_id === version.id ? <RegionPreview version={version} location={evidence} tablePolygon={evidence.table_polygon} /> : <p className="multimodal-warning">此建议来自另一图片版本，请在对应版本查看证据。</p>}
  </div>;
}
