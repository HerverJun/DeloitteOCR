import { useEffect, useState } from "react";
import { Button } from "@fluentui/react-components";
import { api } from "./api";
import "./structure.css";

export type DocumentReviewTask = { id: string; page_id: string; page_number: number; image_id: string | null;
  result_id: string | null; revision: number | null; kind: string; reason: string; category: string; state: string;
  proposal_ids?: string[]; proposal_id?: string; task_id?: string; issue_id?: string; target: { kind: string; tables?: number[]; table_id?: string; row?: number; column?: number } };
export const documentReviewTab = (kind: string) => kind === "fusion" ? "review" : ["multimodal", "multimodal_task"].includes(kind) ? "multimodal" : "structure";
type Queue = { total: number; tasks: DocumentReviewTask[]; summary: { pages: number; unprocessed_pages: number; candidate_pages: number }; timing: { active_ms: number } };

export function DocumentReviewQueue({ documentId, busy, onOpen, onCheck, onError }: { documentId: string; busy: boolean;
  onOpen: (task: DocumentReviewTask) => Promise<unknown>; onCheck: () => Promise<unknown>; onError: (error: string) => void }) {
  const [queue,setQueue] = useState<Queue | null>(null), [offset,setOffset] = useState(0), [state,setState] = useState("open");
  const [working,setWorking] = useState(false), [epoch,setEpoch] = useState(0);
  const [opened, setOpened] = useState<{ id: string; position: number } | null>(null);
  useEffect(() => { setOffset(0); setQueue(null); setOpened(null); }, [documentId,state]);
  useEffect(() => {
    let active = true; let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try { const value = await api<Queue>(`/documents/${documentId}/review-queue?offset=${offset}&limit=10&state=${state}`);
        if (active) { setQueue(value); if (!value.tasks.length && value.total && offset >= value.total) setOffset(Math.floor((value.total-1)/10)*10); }
      } catch(e) { if (active) onError(String(e)); }
      if (active) timer = setTimeout(() => void load(),4000);
    }
    void load(); return () => { active = false; clearTimeout(timer); };
  }, [documentId,offset,state,epoch]);
  async function run(work: () => Promise<unknown>) {
    if (working || busy) return; setWorking(true);
    try { await work(); setEpoch(n => n+1); } catch(e) { onError(String(e)); } finally { setWorking(false); }
  }
  async function openTask(task: DocumentReviewTask, position: number) {
    await onOpen(task);
    setOpened({ id: task.id, position });
  }
  async function openNext() {
    if (!queue?.total) return;
    const previous = queue.tasks.findIndex(task => task.id === opened?.id);
    // If a completed item disappeared, its successor now occupies its position.
    const position = (previous >= 0 ? offset + previous + 1 : opened?.position ?? offset) % queue.total;
    const nextOffset = Math.floor(position / 10) * 10;
    const nextQueue = nextOffset === offset ? queue : await api<Queue>(
      `/documents/${documentId}/review-queue?offset=${nextOffset}&limit=10&state=${state}`,
    );
    const task = nextQueue.tasks[position - nextOffset];
    if (!task) throw Error("复核队列已更新，请重新点击下一处。");
    await openTask(task, position);
    if (nextOffset !== offset) { setOffset(nextOffset); setQueue(nextQueue); }
  }
  return <section className="document-review" aria-label="文档复核队列">
    <strong>文档复核 · {queue?.total ?? "…"} 项</strong>
    <div className="document-actions"><Button size="small" disabled={busy || working} onClick={() => void run(async () => {
      await onCheck(); await api(`/documents/${documentId}/review-queue/check`,"POST",{});
    })}>检查全文候选</Button><Button size="small" disabled={busy || working || !queue?.tasks.length} onClick={() => void run(openNext)}>下一处</Button></div>
    <select aria-label="文档复核筛选" value={state} disabled={working} onChange={e => setState(e.target.value)}><option value="open">待处理与暂缓</option><option value="deferred">仅暂缓</option><option value="all">含已处理</option></select>
    <div className="document-review-list">{queue?.tasks.map((task, index) => <button key={task.id} disabled={busy || working}
      aria-current={task.id === opened?.id ? "true" : undefined} onClick={() => void run(() => openTask(task, offset + index))}>
      <strong>第 {task.page_number} 页 · {task.reason}</strong><small>{task.state === "deferred" || task.state === "question" ? "已暂缓 · " : ""}{task.target.tables?.length ? `表 ${task.target.tables.map(i => i+1).join("、")}` : "页面核对"}</small>
    </button>)}</div>
    {!!queue && queue.total > 10 && <div className="document-actions"><Button size="small" disabled={working || !offset} onClick={() => { setOpened(null); setOffset(n => Math.max(0,n-10)); }}>上一组</Button><span>{offset+1}–{Math.min(offset+10,queue.total)}</span><Button size="small" disabled={working || offset+10 >= queue.total} onClick={() => { setOpened(null); setOffset(n => n+10); }}>下一组</Button></div>}
    <small>{queue?.summary.unprocessed_pages || 0} 页待处理；本地有效复核 {Math.round((queue?.timing.active_ms || 0)/1000)} 秒。队列为空仍需核对整份文档。</small>
  </section>;
}
