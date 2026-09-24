import { useEffect, useRef, useState } from "react";
import { Button } from "@fluentui/react-components";
import { api, downloadPdf } from "./api";
import type { DocumentRecord, DocumentPage } from "./types";
import "./documents.css";
import { Thumbnail } from "./PhotoList";
import { DocumentReviewQueue, type DocumentReviewTask } from "./DocumentReviewQueue";
import { currentDocumentPage, documentPageOffset, type ActiveDocumentPage, type PageSelection } from "./documentNavigation";

const states: Record<string, string> = { pending: "尚未展开", ready: "可处理", processed: "已处理", blank: "空白页", queued: "等待处理", running: "处理中", succeeded: "已完成", waiting_gpu: "区域识别中", paused: "已暂停", interrupted: "等待继续", waiting_unlock: "等待解锁", failed: "失败", cancelled: "已取消" };

export function DocumentTree({ documents, activeImage, activePage, reviewOnly, beforeOpen, onOpen, onRefresh, onError, onReview }:
  { documents: DocumentRecord[]; activeImage: string; activePage: ActiveDocumentPage | null; reviewOnly: boolean; beforeOpen: () => Promise<unknown>;
    onOpen: (imageId: string, stillCurrent?: () => boolean) => Promise<unknown>; onRefresh: () => Promise<unknown>; onError: (error: string) => void;
    onReview: (task: DocumentReviewTask) => Promise<unknown> }) {
  const [documentId, setDocumentId] = useState("");
  const [pages, setPages] = useState<DocumentPage[]>([]);
  const [offset, setOffset] = useState(0);
  const [jump, setJump] = useState("1");
  const [selection, setSelection] = useState<PageSelection | null>(null);
  const [mode, setMode] = useState(reviewOnly ? "native" : "auto");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [query, setQuery] = useState("");
  const [exportRange, setExportRange] = useState("");
  const [exportError, setExportError] = useState("");
  const [dpi, setDpi] = useState(150);
  const [hits, setHits] = useState<{ page_id: string; page_number: number; image_id: string; snippet: string }[]>([]);
  type Navigation = { sequence: number; fromImage: string };
  const navigation = useRef(0);
  const mounted = useRef(true);
  const pendingOpen = useRef<{ pageId: string; navigation: Navigation } | null>(null);
  const latest = useRef({ onOpen, onRefresh, onError, activeImage });
  latest.current = { onOpen, onRefresh, onError, activeImage };
  const beginNavigation = (): Navigation => ({ sequence: ++navigation.current, fromImage: latest.current.activeImage });
  const isCurrent = (intent: Navigation) => mounted.current && intent.sequence === navigation.current && intent.fromImage === latest.current.activeImage;
  const selectedDoc = documents.find(d => d.id === documentId);
  const visiblePages = pages.filter(page => page.document_id === documentId && page.page_number > offset && page.page_number <= offset + 30);
  const current = currentDocumentPage(documentId, activeImage, visiblePages, selection);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; ++navigation.current; }; }, []);
  useEffect(() => {
    ++navigation.current;
    pendingOpen.current = null;
    setSelection(null);
    setNotice("");
    if (activePage) {
      setDocumentId(activePage.documentId);
      setOffset(documentPageOffset(activePage.pageNumber));
      setJump(String(activePage.pageNumber));
      setHits([]); setPassword("");
    } else setJump("1");
  }, [activeImage, activePage?.documentId, activePage?.pageNumber]);

  useEffect(() => {
    if (documents.length && !documents.some(d => d.id === documentId))
      setDocumentId(documents.find(document => document.id === activePage?.documentId)?.id || documents[0].id);
  }, [documents, documentId]);

  useEffect(() => {
    if (!documentId) return;
    let valid = true;
    let timer: ReturnType<typeof setTimeout>;
    const load = async () => {
      try {
        const value = await api<{ pages: DocumentPage[] }>(`/documents/${documentId}/pages?offset=${offset}&limit=30`);
        if (!valid) return;
        setPages(value.pages);
        const active = value.pages.find(page => page.image_id === latest.current.activeImage);
        if (active) setSelection(old => old?.awaitingOpen && old.fromImage === latest.current.activeImage ? old :
          { page: active, fromImage: latest.current.activeImage, awaitingOpen: false });
        const pending = pendingOpen.current;
        const ready = value.pages.find(p => p.id === pending?.pageId && p.image_id);
        if (pending && ready?.image_id && isCurrent(pending.navigation)) {
          await latest.current.onRefresh();
          if (valid && isCurrent(pending.navigation) && pendingOpen.current === pending) {
            await latest.current.onOpen(ready.image_id, () => valid && isCurrent(pending.navigation));
            if (pendingOpen.current === pending) {
              pendingOpen.current = null;
              if (valid) setNotice("");
            }
          }
        }
      } catch (error) { if (valid) latest.current.onError(String(error)); }
      if (valid) timer = setTimeout(() => void load(), 1200);
    };
    void load();
    return () => { valid = false; clearTimeout(timer); };
  }, [documentId, offset]);

  async function act(work: () => Promise<unknown>) {
    setBusy(true); setNotice("");
    try { await work(); await onRefresh(); } catch (error) { onError(String(error)); }
    finally { setBusy(false); }
  }

  async function openPage(page: DocumentPage, intent = beginNavigation(), renderOptions: { dpi?: number } = {}) {
    await beforeOpen();
    if (!isCurrent(intent)) return;
    setSelection({ page, fromImage: intent.fromImage, awaitingOpen: !page.image_id }); setJump(String(page.page_number));
    if (page.image_id) { pendingOpen.current = null; await onOpen(page.image_id, () => isCurrent(intent)); }
    else {
      const pending = { pageId: page.id, navigation: intent };
      pendingOpen.current = pending;
      try {
        await api(`/pages/${page.id}/render`, "POST", renderOptions);
        if (isCurrent(intent)) setNotice(`正在展开第 ${page.page_number} 页…`);
      } catch (error) {
        if (pendingOpen.current === pending) pendingOpen.current = null;
        throw error;
      }
    }
  }

  async function goTo(number: number) {
    if (!selectedDoc || !Number.isInteger(number) || number < 1 || number > selectedDoc.page_count) throw Error("页码超出文档范围");
    const intent = beginNavigation();
    await beforeOpen();
    if (!isCurrent(intent)) return;
    const value = await api<{ pages: DocumentPage[] }>(`/documents/${documentId}/pages?offset=${number-1}&limit=1`);
    if (!isCurrent(intent)) return;
    if (!value.pages[0]) throw Error("页面不存在，请刷新文档后重试。");
    setOffset(documentPageOffset(number));
    await openPage(value.pages[0], intent);
  }

  function browsePages(nextOffset: number) {
    beginNavigation(); pendingOpen.current = null; setNotice("");
    setSelection(old => old?.awaitingOpen ? null : old);
    setOffset(nextOffset);
  }

  return <section className="document-tree" aria-label="文档与页面">
    <div className="document-heading"><strong>文档与页面</strong><span>{documents.length}</span></div>
    <select aria-label="选择文档" value={documentId} disabled={busy} onChange={e => {
      const next = e.target.value;
      const intent = beginNavigation();
      void act(async () => { await beforeOpen(); if (!isCurrent(intent)) return; pendingOpen.current = null; setDocumentId(next); setSelection(null); setPages([]); setOffset(0); setJump("1"); setHits([]); setPassword(""); });
    }}>
      {documents.map(d => <option key={d.id} value={d.id}>{d.name} · {d.page_count || "待解锁"} 页</option>)}
    </select>
    {selectedDoc?.status === "waiting_unlock" && <form className="document-unlock" onSubmit={e => { e.preventDefault(); void act(async () => {
      await api(`/documents/${documentId}/unlock`, "POST", { password }); setPassword("");
      await api(`/documents/${documentId}/queue/resume`, "POST", {});
    }); }}><label>临时密码<input type="password" autoComplete="off" aria-label="PDF 临时密码" value={password} onChange={e => setPassword(e.target.value)} /></label>
      <Button type="submit" disabled={busy || !password}>解锁</Button><small>密码仅用于本次运行，退出后清除。</small></form>}
    {!!selectedDoc?.page_count && <>
      <form className="document-pagination" onSubmit={e => { e.preventDefault(); void act(() => goTo(Number(jump))); }}>
        <Button size="small" aria-label="上一页" disabled={busy || !current || current.page_number <= 1} onClick={() => void act(() => goTo((current?.page_number || 1)-1))}>‹</Button>
        <input aria-label="跳转页码" type="number" min="1" max={selectedDoc.page_count} value={jump} onChange={e => setJump(e.target.value)} /><span>/ {selectedDoc.page_count}</span><Button size="small" type="submit" disabled={busy}>跳转</Button>
        <Button size="small" aria-label="下一页" disabled={busy || !current || current.page_number >= selectedDoc.page_count} onClick={() => void act(() => goTo((current?.page_number || 1)+1))}>›</Button>
      </form>
      <div className="document-pages" aria-label="文档页面列表">{visiblePages.map(page => <button key={page.id} disabled={busy}
        aria-current={page.id === current?.id ? "page" : undefined}
        onClick={() => void act(() => openPage(page))}>{page.active_version ? <Thumbnail id={page.active_version} /> : <span>{page.page_number}</span>}<strong>第 {page.page_number} 页</strong>
        <small>{states[page.stage_status || page.status] || page.status}</small></button>)}</div>
      {selectedDoc.page_count > 30 && <div className="document-page-batches"><Button size="small" disabled={!offset || busy} onClick={() => browsePages(Math.max(0, offset-30))}>前 30 页</Button><span>{offset+1}–{Math.min(offset+30, selectedDoc.page_count)}</span><Button size="small" disabled={offset+30 >= selectedDoc.page_count || busy} onClick={() => browsePages(offset+30)}>后 30 页</Button></div>}
      {current?.stage_error && <div className="document-stage-error" role="alert"><strong>第 {current.page_number} 页 · {current.stage_phase}</strong><p>{current.stage_error}</p>
        {!current.image_id && <><label>渲染 DPI <input aria-label="降低页面DPI" type="number" min="36" max="1200" value={dpi} onChange={e => setDpi(Number(e.target.value))} /></label><Button size="small" disabled={busy} onClick={() => void act(async () => {
          await openPage(current, beginNavigation(), { dpi });
        })}>调整 DPI 并重试</Button></>}
      </div>}
      <div className="document-process"><select aria-label="页面处理方式" value={mode} onChange={e => setMode(e.target.value)}><option value="auto" disabled={reviewOnly}>自动：原生优先，区域补识别</option><option value="native">仅原生提取</option><option value="ocr" disabled={reviewOnly}>整页重新 OCR</option></select>
        <Button size="small" disabled={busy || !current} onClick={() => void act(async () => {
          const intent = { sequence: navigation.current, fromImage: activeImage };
          await beforeOpen(); if (!isCurrent(intent)) return;
          await api(`/pages/${current!.id}/process`, "POST", { mode, force: mode === "ocr" }); setNotice("已加入页面处理队列");
        })}>处理当前页</Button>
        <Button size="small" disabled={busy} onClick={() => void act(async () => {
          await beforeOpen(); await api(`/documents/${documentId}/process`, "POST", { mode }); setNotice("文档已加入处理队列");
        })}>处理全文</Button>
      </div>
      <div className="document-actions">{[["pause", "暂停"], ["resume", "继续"], ["retry", "重试"], ["cancel", "取消"]].map(([key, label]) => <Button size="small" key={key} disabled={busy} onClick={() => void act(() => api(`/documents/${documentId}/queue/${key}`, "POST", {}))}>{label}</Button>)}</div>
      <DocumentReviewQueue key={documentId} documentId={documentId} busy={busy} onCheck={beforeOpen} onError={onError} onOpen={async task => {
        if (task.result_id && task.image_id) {
          beginNavigation(); pendingOpen.current = null; setSelection(null); setNotice("");
          setOffset(documentPageOffset(task.page_number)); setJump(String(task.page_number));
          await onReview(task);
        } else await goTo(task.page_number);
      }} />
      <form className="document-search" onSubmit={e => { e.preventDefault(); void act(async () => {
        const value = await api<{ matches: typeof hits; total: number }>(`/documents/${documentId}/search?q=${encodeURIComponent(query)}`);
        setHits(value.matches); setNotice(`找到 ${value.total} 项，最多显示前 50 项`);
      }); }}><input aria-label="文档内搜索" placeholder="搜索已采用的内容" value={query} onChange={e => setQuery(e.target.value)} /><Button size="small" type="submit" disabled={busy || !query}>搜索</Button></form>
      {!!hits.length && <div className="document-search-results">{hits.map((hit, n) => <button key={n} onClick={() => void act(() => goTo(hit.page_number))}><strong>第 {hit.page_number} 页</strong><span>{hit.snippet}</span></button>)}</div>}
      <details className="document-pdf-export"><summary>导出此文档为可搜索 PDF</summary>
        <label>页码范围<input aria-label="PDF 导出页码" placeholder="留空为全文；例如 1-3,5" value={exportRange} onChange={e => setExportRange(e.target.value)} /></label>
        <p>使用已采用并保存的校对内容。缺定位页面会列出；可以在此明确选择其它页面。来源清单附在 PDF 内。</p>
        <Button size="small" disabled={busy} onClick={() => void act(async () => {
          setExportError(""); await beforeOpen();
          let page_numbers: number[] | undefined;
          if (exportRange.trim()) {
            const numbers = new Set<number>();
            for (const part of exportRange.split(/[,，]/)) {
              const match = part.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
              if (!match) throw Error("页码格式无效，例如 1-3,5");
              const first = Number(match[1]), last = Number(match[2] || match[1]);
              if (first < 1 || last < first || last > selectedDoc.page_count) throw Error("导出页码超出范围");
              for (let n = first; n <= last; n++) numbers.add(n);
            }
            page_numbers = [...numbers].sort((a,b) => a-b);
          }
          try { await downloadPdf({ document_ids: [documentId], page_numbers }); setNotice("PDF 已导出，包含页面来源清单"); }
          catch (error) { setExportError(String(error)); }
        })}>预检并导出 PDF</Button>
        {exportError && <pre className="document-export-errors" role="alert">{exportError}</pre>}
      </details>
    </>}
    {notice && <p className="document-notice" role="status">{notice}</p>}
  </section>;
}
