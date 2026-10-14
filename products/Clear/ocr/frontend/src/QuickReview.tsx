import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@fluentui/react-components";
import { api } from "./api";
import { engineNames, type Result, type ReviewIssue, type ReviewPage, type Table } from "./types";
import "./fusion.css";
import { RegionPreview } from "./RegionPreview";
import { ReviewClock } from "./reviewClock";
import type { Version } from "./types";

const categories: Record<string, string> = { structure: "结构与缺表", amount: "金额", date: "日期", identifier: "编号", number: "其他数字", empty: "空值", text: "文字" };
const states: Record<string, string> = { pending: "未处理", question: "有疑问", stale: "已过期", resolved: "已处理" };
const reasons: Record<string, string> = { alignment_unreliable: "对应关系不够可靠，保留基准供复核", tie: "支持相同，保留基准", insufficient_evidence: "有效来源或支持不足", numeric_protection: "完整数字字段保护", suggestions_only: "此类别目前提供候选建议", evidence_rule_passed: "已通过当前策略规则，仍未人工确认" };

function Value({ value }: { value: string | Table | null }) {
  const [cellPage, setCellPage] = useState(0);
  if (value === null) return <span className="fusion-muted">无对应内容</span>;
  if (typeof value === "string") return <pre>{value === "" ? "（空字符串）" : value}</pre>;
  const large = value.rows > 30 || value.columns > 20 || value.cells.length > 400;
  return <details className="fusion-table-candidate"><summary>完整候选表 · {value.rows} 行 × {value.columns} 列</summary><div>
    {large ? <><p>大表按 50 格分页显示；采用时仍写入完整候选表。坐标从 1 开始。</p>
      <table><thead><tr><th>行 / 列</th><th>跨行 / 跨列</th><th>内容</th></tr></thead><tbody>
        {value.cells.slice(cellPage * 50, cellPage * 50 + 50).map(c => <tr key={`${c.row}:${c.column}`}><td>{c.row+1} / {c.column+1}</td><td>{c.row_span} / {c.column_span}</td><td>{c.text || "（空值）"}</td></tr>)}
      </tbody></table><Button size="small" disabled={!cellPage} onClick={e => { e.preventDefault(); setCellPage(n => n-1); }}>前 50 格</Button>
      <span> {cellPage+1} / {Math.ceil(value.cells.length/50)} </span><Button size="small" disabled={(cellPage+1)*50 >= value.cells.length} onClick={e => { e.preventDefault(); setCellPage(n => n+1); }}>后 50 格</Button></> :
    <table><tbody>{Array.from({ length: value.rows }, (_, r) => <tr key={r}>
      {value.cells.filter(c => c.row === r).sort((a, b) => a.column - b.column).map(c => <td key={c.column} rowSpan={c.row_span} colSpan={c.column_span}>{c.text || "\u00a0"}</td>)}
    </tr>)}</tbody></table>}
  </div></details>;
}

export function QuickReview({ result, versionId, version, geometryRefresh = 0, adopted, busy, onDecision, onLocate, onConfirm, onDraft, getDraft, getPendingDecision }: {
  result: Result; versionId: string; adopted: boolean; busy: boolean;
  version?: Version | null; geometryRefresh?: number;
  onDecision: (issueId: string, body: Record<string, unknown>) => Promise<void>;
  onLocate: (issue: ReviewIssue, edit: boolean, reveal?: boolean) => void;
  onConfirm: () => void;
  onDraft: (draft: { issueId: string; value: string } | null) => void;
  getDraft: () => { issueId: string; value: string } | null;
  getPendingDecision: () => { issueId: string; body: Record<string, unknown> } | null;
}) {
  const [page, setPage] = useState<ReviewPage | null>(null);
  const [filter, setFilter] = useState("");
  const [category, setCategory] = useState("");
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);
  const [manual, setManual] = useState("");
  const [manualDirty, setManualDirty] = useState(false);
  const [chosen, setChosen] = useState("");
  const [placement, setPlacement] = useState("end");
  const composing = useRef(false);
  const generation = useRef(0);
  const offset = useRef(0);
  const locking = useRef(false);
  const intent = useRef<{ issueId: string; body: Record<string, unknown> } | null>(getPendingDecision());
  const activeIssue = page?.issues[0];
  const clock = useRef(new ReviewClock());
  const ready = useCallback((value: boolean) => clock.current.setReady(value), []);
  useEffect(() => {
    const timer = setInterval(() => clock.current.tick(), 1000);
    const c = clock.current;
    document.addEventListener("visibilitychange", c.visibility);
    window.addEventListener("pointerdown", c.interact);
    window.addEventListener("keydown", c.interact);
    window.addEventListener("pointermove", c.interact);
    return () => { clearInterval(timer); document.removeEventListener("visibilitychange", c.visibility); window.removeEventListener("pointerdown", c.interact); window.removeEventListener("keydown", c.interact); window.removeEventListener("pointermove", c.interact); };
  }, []);
  useEffect(() => { clock.current.setWaiting(busy || working); }, [busy, working]);
  useEffect(() => { clock.current.reset(); }, [activeIssue?.id]);
  const load = useCallback(async (at: number, resume = false) => {
    const request = ++generation.current;
    const query = new URLSearchParams({ offset: String(Math.max(0, at)), limit: "1", resume: String(resume) });
    if (filter) query.set("state", filter);
    if (category) query.set("category", category);
    let next = await api<ReviewPage>(`/results/${result.id}/issues?${query}`);
    if (!next.issues.length && next.total && at >= next.total) {
      query.set("offset", String(next.total - 1));
      next = await api<ReviewPage>(`/results/${result.id}/issues?${query}`);
    }
    if (request !== generation.current) return;
    setPage(next);
    offset.current = next.offset;
    if (next.issues[0]) {
      await api(`/results/${result.id}/issues/position`, "PUT", { issue_id: next.issues[0].id });
      if (request === generation.current) onLocate(next.issues[0], false);
    }
  }, [result.id, filter, category]);
  useEffect(() => {
    void load(0, true).catch(e => setError(String(e)));
    return () => { ++generation.current; };
  }, [load]);
  useEffect(() => { if (page && !locking.current) void load(offset.current).catch(e => setError(String(e))); }, [geometryRefresh]);
  useEffect(() => {
    if (!page || locking.current) return;
    intent.current = getPendingDecision();
    const draft = getDraft();
    setManualDirty(!!draft);
    if (!intent.current && !draft) {
      setError("");
      setChosen("");
      void load(offset.current).catch(e => setError(String(e)));
    }
  }, [result, versionId, adopted]);
  useEffect(() => {
    if (!activeIssue) return;
    const draft = getDraft();
    setManual(draft?.issueId === activeIssue.id ? draft.value : typeof activeIssue.current_value === "string" ? activeIssue.current_value : "");
    setManualDirty(draft?.issueId === activeIssue.id);
    setChosen("");
    onLocate(activeIssue, false);
  }, [activeIssue?.id, activeIssue?.current_fingerprint]);
  const move = async (direction: number) => {
    if (locking.current || manualDirty || intent.current) return;
    locking.current = true; setWorking(true); setError("");
    try { await load(offset.current + direction); }
    catch (e) { setError(String(e)); }
    finally { locking.current = false; setWorking(false); }
  };
  const save = async (action: string) => {
    if (locking.current || composing.current || !activeIssue) return;
    locking.current = true; setWorking(true); setError("");
    try {
      if (!intent.current) intent.current = { issueId: activeIssue.id, body: {
        request_id: crypto.randomUUID(), action, basis: activeIssue.basis,
        current_fingerprint: activeIssue.current_fingerprint, version_id: versionId,
        ...(action === "candidate" ? { candidate_id: chosen, ...(activeIssue.target.kind === "unmatched_table" ? { placement } : {}) } : {}), ...(action === "manual" ? { value: manual } : {}),
      } };
      await onDecision(intent.current.issueId, intent.current.body);
      const timingAction = String(intent.current.body.action);
      // Timing failure must never turn a successful edit into a failed save.
      try {
        const saved = await api<Result>(`/results/${result.id}`);
        await api(`/results/${result.id}/review-timing`, "POST", { id: String(intent.current.body.request_id), revision: saved.revision,
          target: activeIssue.target, active_ms: Math.round(clock.current.activeMs), action: timingAction });
      } catch { /* The edit is durable; optional local timing is omitted. */ }
      intent.current = null;
      setManualDirty(false);
      onDraft(null);
      await load(filter ? offset.current : offset.current + 1);
    } catch (e) { setError(String(e)); }
    finally { locking.current = false; setWorking(false); }
  };
  const submitting = !!intent.current;
  const disabled = busy || working || submitting || !adopted || !page?.context_current;
  const needsTableEditor = !!activeIssue && ["text", "document"].includes(activeIssue.target.kind) && result.edited.tables.length > 0;
  return <section className="quick-review" aria-label="快速校对">
    <div className="fusion-summary"><strong>逐项核对来源分歧</strong>
      <span>{Object.entries(page?.counts || {}).map(([s, n]) => `${states[s]} ${n}`).join(" · ")}</span>
    </div>
    <p className="fusion-muted">提示处理完后，仍需确认当前图片。支持份数表示来源证据，不是正确概率。</p>
    {!!result.original.fusion?.structure_fallbacks?.length && <p className="inline-warning">基准结构不完整，已从身份匹配可靠的来源选择完整骨架；相关整表必须复核。原基准和完整候选均保留在来源证据中。</p>}
    {!adopted && <p className="inline-warning">先采用此预览，再提交校对决策。</p>}
    <div className="fusion-filters">
      <label>状态 <select aria-label="疑点状态" value={filter} disabled={working || manualDirty || submitting} onChange={e => setFilter(e.target.value)}><option value="">全部</option>{Object.entries(states).map(([k,v]) => <option key={k} value={k}>{v}</option>)}</select></label>
      <label>类型 <select aria-label="疑点类型" value={category} disabled={working || manualDirty || submitting} onChange={e => setCategory(e.target.value)}><option value="">全部类型</option>{Object.entries(categories).map(([k,v]) => <option key={k} value={k}>{v}</option>)}</select></label>
      <span>{page?.total ? `${page.offset+1} / ${page.total}` : "0 项"}</span>
    </div>
    {error && <div className="inline-warning" role="alert">{error}<p>修改与当前位置已保留。可重试保存；发生版本冲突时，请保留草稿并重新加载结果。</p></div>}
    {submitting && <div className="inline-warning">上次提交尚未核实，选择已锁定。重试会发送相同内容；成功后再继续校对。<Button disabled={busy || working || !adopted || !page?.context_current} onClick={() => void save("retry")}>重试上次提交</Button></div>}
    {activeIssue ? <>
      <div className="fusion-issue-heading"><strong>{categories[activeIssue.category]} · {states[activeIssue.state]}</strong><Button size="small" disabled={working || manualDirty || submitting} onClick={() => onLocate(activeIssue, true)}>在编辑器定位</Button></div>
      <p>{activeIssue.reason === "baseline_structure_invalid" ? "基准表格结构不完整。请核对原始来源、完整候选表与阅读位置；没有可靠骨架时仅保留原始文字。" : reasons[activeIssue.reason] || activeIssue.reason}</p>
      <p className="fusion-location">定位：{activeIssue.location.level === "cell" ? "单元格" : activeIssue.location.level === "region" ? "整表 / 文字区域" : "全图"} · {activeIssue.location.reason} <Button size="small" onClick={() => onLocate(activeIssue, false, true)}>定位原图</Button></p>
      {version && <RegionPreview version={version} location={activeIssue.location} tablePolygon={activeIssue.location.table_polygon} onReady={ready} />}
      {activeIssue.context && <div className="review-context"><p>行标题：{activeIssue.context.row_header || "未确定"}</p><p>列标题：{activeIssue.context.column_header || "未确定"}</p><p className="neighbors">相邻内容：{activeIssue.context.neighbors.map(n => `第 ${n.row+1} 行 ${n.column+1} 列：${n.text || "空值"}`).join("；") || "无"}</p></div>}
      <div className="fusion-current"><small>当前已保存值</small><Value value={activeIssue.current_value} /></div>
      {needsTableEditor && <p className="inline-warning">此整段包含结构化表格，候选仅供对照。请点击「在编辑器定位」核对文字和表格，再返回「保留当前并继续」；也可先标记「暂不确定」。</p>}
      <div className="fusion-candidates" role="radiogroup" aria-label="原始来源候选">
        {activeIssue.candidates.map(candidate => <label key={candidate.id} className={chosen === candidate.id ? "selected" : ""}>
          <input type="radio" name="fusion-candidate" value={candidate.id} checked={chosen === candidate.id} disabled={disabled || needsTableEditor || activeIssue.state === "stale"} onChange={() => setChosen(candidate.id)} />
          <div><small>{candidate.sources.map(e => engineNames[e] || e).join("、")} · {candidate.sources.length}/{activeIssue.expected_sources.length} 个预期来源支持</small><Value value={candidate.value} /></div>
        </label>)}
      </div>
      {activeIssue.target.kind === "unmatched_table" && <label>完整候选表的插入位置 <select aria-label="缺表插入位置" value={placement} disabled={disabled} onChange={e => setPlacement(e.target.value)}><option value="end">文档末尾</option><option value="start">文档开头</option></select><p>请先核对原图的阅读顺序；插入后可在普通编辑器继续调整。</p></label>}
      <details><summary>来源覆盖与原始依据</summary><p>{Object.entries(activeIssue.source_states).map(([engine, state]) => `${engineNames[engine] || engine}：${({succeeded:"有效",failed:"失败",cancelled:"取消",unsupported:"不支持结构",unmatched:"未匹配"} as Record<string,string>)[state] || state}`).join("；")}</p><small>疑点 {activeIssue.id} · 策略 {result.original.fusion?.policy.version}</small></details>
      {typeof activeIssue.current_value === "string" && !needsTableEditor && <label className="fusion-manual">手工修改<textarea aria-label="疑点手工修改" value={manual} disabled={disabled || activeIssue.state === "stale"} onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }} onChange={e => { setManual(e.target.value); setManualDirty(true); onDraft({ issueId: activeIssue.id, value: e.target.value }); }} /></label>}
      {manualDirty && <p className="fusion-muted">手工修改尚未保存；保存成功后再切换疑点。<Button size="small" disabled={working || submitting} onClick={() => { setManual(typeof activeIssue.current_value === "string" ? activeIssue.current_value : ""); setManualDirty(false); onDraft(null); }}>清除手工修改</Button></p>}
      <div className="fusion-actions">
        <Button appearance="primary" disabled={disabled || needsTableEditor || (!chosen && !manualDirty) || activeIssue.state === "stale"} onClick={() => void save(manualDirty ? "manual" : "candidate")}>保存并继续</Button>
        <Button disabled={disabled || manualDirty} onClick={() => void save("keep")}>保留当前并继续</Button>
        <Button disabled={disabled || manualDirty} onClick={() => void save("question")}>暂不确定</Button>
        <Button disabled={working || manualDirty || submitting || !page?.offset} onClick={() => void move(-1)}>上一项</Button>
        <Button disabled={working || manualDirty || submitting || !page || page.offset+1 >= page.total} onClick={() => void move(1)}>下一项（跳过）</Button>
      </div>
    </> : <p className="fusion-empty">当前筛选没有疑点。提示为空不代表原文没有遗漏错误。</p>}
    <Button disabled={disabled || manualDirty} onClick={onConfirm}>确认当前图片</Button>
  </section>;
}
