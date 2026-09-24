import { useEffect, useRef, useState } from "react";
import { Button, Checkbox, Field, Input, Select, Textarea } from "@fluentui/react-components";
import { MessageSquare, Settings2, X } from "lucide-react";
import { api, request } from "./api";
import { AgentConnection } from "./AgentConnection";
import { AgentBudget } from "./AgentBudget";
import { AgentArtifact } from "./AgentArtifact";
import { AgentToolResult } from "./AgentToolResult";
import { agentRequestId, readAgentStream } from "./agentApi";
import type { AgentConnectionState, AgentDestination, AgentEvent, AgentEvidence, AgentResult, AgentRun, AgentSession } from "./agentTypes";
import type { DocumentRecord } from "./types";
import "./agent.css";

const states: Record<string, string> = { queued: "排队中", running: "正在处理", waiting_jobs: "等待后台任务", waiting_user: "等待你的回复", completed: "已结束", failed: "未完成", cancelled: "已停止", interrupted: "重启后等待继续" };
const inboxStates: Record<string, string> = { pending: "排队等待安全边界", applied: "已在安全边界处理", discarded: "运行已停止 · 未执行" };
const tools: Record<string, string> = { get_workspace_context: "查看项目", search_document: "查找文档内容", read_page_result: "读取页面", navigate_to_evidence: "定位证据", ask_user: "等待说明", process_pages: "处理页面", run_ocr: "文字识别", get_job_status: "核对后台进度", export_results: "创建导出", inspect_table: "检查表格", request_visual_review: "视觉审校", retry_failed_jobs: "重试未完成任务" };

export function AgentPanel({ projectId, selectedImageIds, documents, engines, onNavigate }: { projectId: string; selectedImageIds: string[]; documents: DocumentRecord[]; engines: string[]; onNavigate: (ref: AgentEvidence, destination?: AgentDestination) => Promise<void> }) {
  const [enabled, setEnabled] = useState(false);
  const [available, setAvailable] = useState(false);
  const [open, setOpen] = useState(false);
  const [configure, setConfigure] = useState(false);
  const [connection, setConnection] = useState<AgentConnectionState | null>(null);
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [moreSessions, setMoreSessions] = useState(false);
  const [sessionId, setSessionId] = useState("");
  const [sessionTitle, setSessionTitle] = useState("");
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [run, setRun] = useState<AgentRun | null>(null);
  const [inbox, setInbox] = useState<{ id: number; run_id: string; state: string; scope_error?: string | null }[]>([]);
  const [content, setContent] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [connected, setConnected] = useState(false);
  const [streamError, setStreamError] = useState("");
  const [authorized, setAuthorized] = useState(false);
  const [allowProcessing, setAllowProcessing] = useState(false);
  const [allowReprocess, setAllowReprocess] = useState(false);
  const [allowPartial, setAllowPartial] = useState(false);
  const [allowStructure, setAllowStructure] = useState(false);
  const [allowRetry, setAllowRetry] = useState(false);
  const [visualModel, setVisualModel] = useState("");
  const [allowVisualImages, setAllowVisualImages] = useState(false);
  const [visualModels, setVisualModels] = useState<{ id: string; label: string; available: boolean; base_url?: string }[]>([]);
  const [documentId, setDocumentId] = useState("");
  const [pageRange, setPageRange] = useState("1");
  const [refresh, setRefresh] = useState(0);
  const mountedProject = useRef(projectId); mountedProject.current = projectId;
  const mountedSession = useRef(sessionId); mountedSession.current = sessionId;
  const lastSeq = useRef(0);
  const snapshotSeq = useRef(0);
  const pendingSend = useRef<{ key: string; content: string; scope: string; selectionToken: string } | null>(null);
  const pendingMutations = useRef(new Map<string, string>());
  const actionToken = useRef<symbol | null>(null);
  const historyWindow = useRef(500);
  const updateRun = (value: AgentRun) => setRun(previous => previous?.id === value.id &&
    (value.generation > previous.generation || value.generation === previous.generation && value.last_event_seq >= previous.last_event_seq) ? value : previous);
  const mutate = async <T,>(url: string, method: string, payload: Record<string, unknown>): Promise<T> => {
    const key = JSON.stringify([url, method, payload]);
    const requestId = pendingMutations.current.get(key) || agentRequestId();
    pendingMutations.current.set(key, requestId);
    const value = await api<T>(url, method, { ...payload, client_request_id: requestId });
    pendingMutations.current.delete(key);
    return value;
  };
  useEffect(() => {
    let alive = true;
    void api("/agent/status").then(v => { if (alive) { setEnabled(v.enabled); setAvailable(v.available); } }).catch(() => undefined);
    return () => { alive = false; };
  }, [refresh]);
  useEffect(() => {
    setSessionId(""); setSessions([]); setEvents([]); setRun(null); setContent(""); setAuthorized(false); setError(""); setBusy(false); pendingSend.current = null; actionToken.current = null;
    setDocumentId(""); setPageRange("1");
    setVisualModel(""); setAllowVisualImages(false); setAllowStructure(false);
    setAllowRetry(false); setAllowProcessing(false); setAllowReprocess(false); setAllowPartial(false);
  }, [projectId]);
  useEffect(() => {
    if (!open || !projectId) return;
    const controller = new AbortController();
    void Promise.all([
      request(`/projects/${projectId}/agent/sessions`, { signal: controller.signal }).then(r => r.json()),
      request("/agent/connection", { signal: controller.signal }).then(r => r.json()),
      request("/multimodal/models", { signal: controller.signal }).then(r => r.json()),
      request(`/projects/${projectId}/agent/controller-authorization`, { signal: controller.signal }).then(r => r.json()),
    ]).then(([history, config, visual, authorization]) => {
      if (controller.signal.aborted) return;
      setSessions(history.sessions); setConnection(config);
      setMoreSessions(history.sessions.length === 30);
      setAuthorized(authorization.authorized && authorization.revision === config.revision);
      setVisualModels(visual.models || []);
      setSessionId(previous => history.sessions.some((s: AgentSession) => s.id === previous) ? previous : history.sessions[0]?.id || "");
    }).catch(e => { if (!controller.signal.aborted) setError(String(e)); });
    return () => controller.abort();
  }, [open, projectId, refresh]);
  useEffect(() => { setSessionTitle(sessions.find(s => s.id === sessionId)?.title || ""); }, [sessionId, sessions]);
  useEffect(() => {
    setEvents([]); setRun(null); setInbox([]); setConnected(false); setStreamError(""); lastSeq.current = 0; snapshotSeq.current = 0; historyWindow.current = 500;
    if (!open || !sessionId) return;
    const controller = new AbortController();
    const expire = () => { controller.abort(); setConnected(false); setError("会话鉴权已失效，请从托盘重新打开工作台。"); };
    window.addEventListener("ocr-session-expired", expire);
    const snapshot = () => request(`/agent/sessions/${sessionId}`, { signal: controller.signal }).then(r => r.json()).then(v => { if (!controller.signal.aborted && v.last_seq >= snapshotSeq.current) { snapshotSeq.current = v.last_seq; setRun(v.runs[0] || null); setInbox(v.inbox || []); } return v; });
    const receive = (event: AgentEvent) => {
      if (controller.signal.aborted || event.seq <= lastSeq.current) return;
      lastSeq.current = event.seq;
      setEvents(previous => [...previous, event].slice(-historyWindow.current));
      if (["run_state", "budget_updated", "tool_finished", "job_progress", "message"].includes(event.type)) void snapshot().catch(() => undefined);
    };
    const follow = async () => {
      try {
        const initial = await snapshot();
        const after = Math.max(0, initial.last_seq - 500);
        const response = await request(`/agent/sessions/${sessionId}/events?after_seq=${after}&limit=500`, { signal: controller.signal });
        const history = await response.json();
        if (controller.signal.aborted) return;
        setEvents(history.events); lastSeq.current = history.next_seq;
      } catch (e) { if (controller.signal.aborted) return; setError(String(e)); }
      while (!controller.signal.aborted) {
        try { await readAgentStream(sessionId, lastSeq.current, controller.signal, receive, () => { if (!controller.signal.aborted) { setConnected(true); setStreamError(""); } }); }
        catch { if (!controller.signal.aborted) setStreamError("进度连接暂时中断，正在重连；后台运行不受影响。"); }
        if (controller.signal.aborted) break;
        setConnected(false);
        await new Promise<void>(resolve => {
          const finish = () => { clearTimeout(timer); controller.signal.removeEventListener("abort", finish); resolve(); };
          const timer = setTimeout(finish, 1500); controller.signal.addEventListener("abort", finish, { once: true });
        });
      }
    };
    void follow();
    return () => { controller.abort(); window.removeEventListener("ocr-session-expired", expire); };
  }, [sessionId, open]);
  const action = async (work: () => Promise<void>) => {
    if (actionToken.current) return;
    const token = Symbol(); actionToken.current = token;
    setBusy(true); setError(""); const project = projectId;
    try { await work(); } catch (e) { if (mountedProject.current === project) setError(String(e)); }
    finally { if (actionToken.current === token) { actionToken.current = null; if (mountedProject.current === project) setBusy(false); } }
  };
  const createSession = async () => {
    const value = await mutate<AgentSession>(`/projects/${projectId}/agent/sessions`, "POST", { title: "文档助手" });
    if (mountedProject.current !== projectId) return "";
    setSessions(previous => [value, ...previous]); setSessionId(value.id); return value.id;
  };
  const send = () => {
    if (!canSend) return;
    return action(async () => {
    const current = sessionId || await createSession(); if (!current) return;
    const ranges = documentId ? pageRange.split(/[,，]/).map(value => {
      const match = value.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
      if (!match) throw new Error("页码请填写 1-3,8 这样的范围");
      return { document_id: documentId, first_page: Number(match[1]), last_page: Number(match[2] ?? match[1]) };
    }) : [];
    const scope = JSON.stringify({ session: current, selectedImageIds, ranges, engines, allowProcessing, allowReprocess, allowPartial, allowStructure, allowRetry, visualModel, allowVisualImages });
    if (!pendingSend.current || pendingSend.current.content !== content || pendingSend.current.scope !== scope) {
      const selection = await api<{ selection_token: string }>(`/projects/${projectId}/agent/selection`, "POST", { image_ids: selectedImageIds, engines,
        document_ranges: ranges, allow_processing: allowProcessing, allow_reprocess: allowReprocess, allow_export: true, allow_partial: allowPartial,
        allow_structure_checks: allowStructure, allow_retry_failed: allowRetry, visual_model_id: visualModel || null, allow_visual_images: allowVisualImages });
      pendingSend.current = { key: agentRequestId(), content, scope, selectionToken: selection.selection_token };
    }
    const value = await api<{ run: AgentRun }>(`/agent/sessions/${current}/messages`, "POST", { client_request_id: pendingSend.current.key, content: pendingSend.current.content, selection_token: pendingSend.current.selectionToken });
    if (mountedProject.current !== projectId || mountedSession.current !== current) return;
    setRun(value.run); setContent(""); pendingSend.current = null;
    });
  };
  const active = !!run && !["completed", "cancelled", "failed"].includes(run.status);
  const selectedSession = sessions.find(s => s.id === sessionId);
  const canSend = !busy && available && !!connection?.available && authorized && selectedSession?.status !== "archived" && !!content.trim();
  const updateSession = (changes: { title?: string; status?: "active" | "archived" }) => action(async () => {
    const updated = await mutate<AgentSession>(`/agent/sessions/${sessionId}`, "PATCH", changes);
    if (mountedProject.current === projectId) setSessions(previous => previous.map(s => s.id === updated.id ? updated : s));
  });
  const resolved = new Set(events.filter(e => e.type === "decision_resolved").map(e => String(e.payload.decision_id)));
  if (!enabled) return null;
  return <>
    <Button className="agent-launch" icon={<MessageSquare size={17} />} disabled={!projectId} onClick={() => setOpen(true)}>文档助手</Button>
    {open && <aside className="agent-panel" aria-label="文档助手" onKeyDown={e => { if (e.key === "Escape") setOpen(false); }}>
      <header><div><strong>文档助手</strong><small>{connected ? "进度已连接" : "可查看历史记录"}</small></div><Button appearance="subtle" icon={<Settings2 size={17} />} aria-label="配置主控" onClick={() => setConfigure(true)} /><Button appearance="subtle" icon={<X size={18} />} aria-label="关闭助手面板" onClick={() => setOpen(false)} /></header>
      <div className="agent-session-row"><Select aria-label="历史会话" value={sessionId} onChange={(_, d) => setSessionId(d.value)}><option value="">新会话</option>{sessions.map(s => <option key={s.id} value={s.id}>{s.title}</option>)}</Select><Button size="small" disabled={busy || !available} onClick={() => void action(async () => { await createSession(); })}>新建</Button></div>
      {moreSessions && <Button size="small" disabled={busy} onClick={() => void action(async () => {
        const page = await api<{ sessions: AgentSession[] }>(`/projects/${projectId}/agent/sessions?offset=${sessions.length}&limit=30`);
        if (mountedProject.current !== projectId) return;
        setSessions(previous => [...previous, ...page.sessions.filter(s => !previous.some(old => old.id === s.id))]);
        setMoreSessions(page.sessions.length === 30);
      })}>更多历史会话</Button>}
      {selectedSession && <details className="agent-session-settings"><summary>会话管理{selectedSession.status === "archived" ? " · 已归档" : ""}</summary><Field label="会话名称"><Input value={sessionTitle} maxLength={200} onChange={(_, d) => setSessionTitle(d.value)} /></Field><Button size="small" disabled={busy || !sessionTitle.trim()} onClick={() => void updateSession({ title: sessionTitle.trim() })}>保存名称</Button><Button size="small" disabled={busy || active} onClick={() => void updateSession({ status: selectedSession.status === "active" ? "archived" : "active" })}>{selectedSession.status === "active" ? "归档会话" : "恢复会话"}</Button></details>}
      {!available && <p role="status">助手运行依赖不可用。普通识别、校对和导出可继续使用。</p>}
      {!connection?.available && <p>先<Button appearance="transparent" onClick={() => setConfigure(true)}>配置主控连接</Button>，再查询当前项目。</p>}
      {connection?.available && !authorized && <div className="agent-consent"><p>允许主控 <strong>{connection.model}</strong> 按需读取当前项目文字与文档信息？图片使用独立视觉审校授权。</p><code>{connection.base_url}</code><Button size="small" disabled={busy} onClick={() => void action(async () => { await api(`/projects/${projectId}/agent/controller-authorization`, "PUT", { revision: connection.revision, allow: true }); setAuthorized(true); })}>允许此地址读取当前项目</Button></div>}
      <div className="agent-transcript" aria-live="polite" aria-relevant="additions">
        {!!events.length && events[0].seq > 1 && <Button size="small" disabled={busy} onClick={() => void action(async () => {
          const after = Math.max(0, events[0].seq - 501);
          const older = await api<{ events: AgentEvent[] }>(`/agent/sessions/${sessionId}/events?after_seq=${after}&limit=500`);
          if (mountedProject.current !== projectId || mountedSession.current !== sessionId) return;
          historyWindow.current += 500;
          setEvents(previous => [...older.events.filter(e => !previous.some(p => p.seq === e.seq)), ...previous].sort((a, b) => a.seq - b.seq));
        })}>加载更早记录</Button>}
        {!events.length && <p className="agent-empty">描述你要找的内容，或指定文档和页码。助手会列出实际步骤与证据。</p>}
        {events.map(event => {
          const p = event.payload;
          if (event.type === "message") return <div className={`agent-message agent-${String(p.role)}`} key={event.seq}><small>{p.role === "user" ? "你" : "助手"}{p.inbox_id ? " · " + (inboxStates[inbox.find(i => i.id === Number(p.inbox_id))?.state || "pending"] || "等待状态更新") : ""}</small><p>{String(p.content || "")}</p></div>;
          if (event.type === "tool_finished") {
            const result = p.result as AgentResult;
            return <div className="agent-tool" key={event.seq}><small>{tools[String(p.tool)] || "工具操作"}</small><p>{result.summary}</p><AgentToolResult result={result} navigate={(ref, destination) => void action(() => onNavigate(ref, destination))} />{result.evidence_refs.map(ref => <Button key={ref.ref_id} size="small" appearance="subtle" onClick={() => void action(() => onNavigate(ref))}>第 {ref.page_number} 页 · {ref.is_adopted ? "采用结果" : "本轮结果"}</Button>)}</div>;
          }
          if (event.type === "decision_required" && !resolved.has(String(p.decision_id)) && event.run_id === run?.id && event.generation === run.generation && run.status === "waiting_user") return <div className="agent-decision" key={event.seq}><p>{String(p.question)}</p>{(p.options as { id: string; label: string }[]).map(option => <Button key={option.id} size="small" disabled={busy} onClick={() => void action(async () => { await api(`/agent/decisions/${String(p.decision_id)}/reply`, "POST", { client_request_id: `reply-${String(p.decision_id)}-${option.id}`, payload_hash: p.payload_hash, option_id: option.id }); })}>{option.label}</Button>)}</div>;
          if (event.type === "error") return <p role="alert" key={event.seq}>{String(p.message)}</p>;
          if (event.type === "job_progress") return <div className="agent-tool" key={event.seq}><small>后台进度</small><p>{String(p.summary)}</p></div>;
          if (event.type === "artifact_ready") return <AgentArtifact key={event.seq} projectId={projectId} id={String(p.artifact_id)} busy={busy} action={action} />;
          return null;
        })}
      </div>
      {run && <div className="agent-run-state"><span>{states[run.status] || run.status}{run.outcome === "partial" ? " · 有未覆盖内容" : ""}</span>{active && <Button size="small" disabled={busy} onClick={() => void action(async () => { updateRun(await mutate<AgentRun>(`/agent/runs/${run.id}/cancel`, "POST", { generation: run.generation, mode: "stop_agent" })); })}>停止助手</Button>}{run.status === "interrupted" && <Button size="small" disabled={busy} onClick={() => void action(async () => { updateRun(await mutate<AgentRun>(`/agent/runs/${run.id}/resume`, "POST", { generation: run.generation })); })}>继续</Button>}</div>}
      {error && <p className="agent-error" role="alert">{error}</p>}
      {streamError && <p role="status">{streamError}</p>}
      {inbox.filter(i => i.run_id === run?.id && i.scope_error).map(i => <p className="agent-error" key={i.id}>追加范围未生效：{i.scope_error}</p>)}
      <details className="agent-scope-settings"><summary>范围、审校与预算</summary>
      {run && <AgentBudget key={run.id} run={run} onUpdated={updateRun} action={action} busy={busy} />}
      <Checkbox label="允许本轮按原范围重试失败任务（仍受预算限制）" checked={allowRetry} onChange={(_, d) => setAllowRetry(d.checked === true)} />
      {!!run?.coverage.requested_count && <p>本轮 {Number(run.coverage.requested_count)} 页 · 完整覆盖 {Number(run.coverage.completed_count ?? 0)} · 等待 {Number(run.coverage.pending_count ?? 0)} · 失败 {Number(run.coverage.failed_count ?? 0)} · 未覆盖 {Number(run.coverage.uncovered_count ?? 0)}</p>}
      <Field label="追加文档范围（支持未渲染页面）"><Select value={documentId} onChange={(_, d) => setDocumentId(d.value)}><option value="">仅使用工作台已选页面</option>{documents.filter(d => d.kind !== "image").map(d => <option key={d.id} value={d.id}>{d.name} · {d.page_count} 页</option>)}</Select></Field>
      {documentId && <Field label="文档页码，例如 1-3,8"><Input value={pageRange} onChange={(_, d) => setPageRange(d.value)} /></Field>}
      <details><summary>所选采用结果的审校权限</summary><Checkbox label="允许生成结构候选（采用仍由我决定）" checked={allowStructure} onChange={(_, d) => setAllowStructure(d.checked === true)} />
        <Field label="允许使用的视觉审校模型"><Select value={visualModel} onChange={(_, d) => { setVisualModel(d.value); setAllowVisualImages(false); }}><option value="">不启用视觉审校</option>{visualModels.map(m => <option key={m.id} value={m.id} disabled={!m.available}>{m.label}</option>)}</Select></Field>
        {visualModel.startsWith("external:") && <><p>将所选采用结果的原图、文字和元数据发送到 {visualModels.find(m => m.id === visualModel)?.base_url}，可能产生费用。</p><Checkbox label="允许本轮向该视觉连接发送上述内容" checked={allowVisualImages} onChange={(_, d) => setAllowVisualImages(d.checked === true)} /></>}
      </details>
      </details>
      {run && active && <Button size="small" disabled={busy} onClick={() => void action(async () => { updateRun(await mutate<AgentRun>(`/agent/runs/${run.id}/cancel`, "POST", { generation: run.generation, mode: "cancel_owned_jobs" })); })}>停止并取消本轮自建任务</Button>}
      <form onSubmit={e => { e.preventDefault(); void send(); }}><Field label="本轮目标"><Textarea value={content} resize="vertical" onChange={(_, d) => setContent(d.value)} onKeyDown={e => { if (e.key !== "Enter" || e.shiftKey || e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return; e.preventDefault(); void send(); }} maxLength={16000} placeholder="例如：找到应收账款那张表" /></Field><details><summary>本轮范围：{selectedImageIds.length} 个已选页面，可查询并创建导出</summary><Checkbox label="允许处理所选页面" checked={allowProcessing} onChange={(_, d) => setAllowProcessing(d.checked === true)} /><Checkbox label="允许重新处理所选页面" disabled={!allowProcessing} checked={allowReprocess} onChange={(_, d) => setAllowReprocess(d.checked === true)} /><Checkbox label="部分失败时允许仅导出成功页" checked={allowPartial} onChange={(_, d) => setAllowPartial(d.checked === true)} /></details><Button type="submit" appearance="primary" disabled={!canSend}>{active ? "追加指令" : "发送"}</Button></form>
      <small className="agent-footnote">关闭面板不会停止后台运行。内容与建议需结合原文核对。</small>
    </aside>}
    {configure && <AgentConnection onClose={() => setConfigure(false)} onSaved={() => { setRefresh(v => v + 1); setAuthorized(false); }} />}
  </>;
}
