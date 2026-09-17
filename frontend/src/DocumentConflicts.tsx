import { useEffect, useState } from "react";
import { Button } from "@fluentui/react-components";
import { api } from "./api";
import type { Result } from "./types";
type Conflict = { id: string; reason?: string; reviewed: boolean; native: { text: string }[]; ocr: { text: string; polygon?: number[][] } };
export function DocumentConflicts({ result, beforeSave, onLocate }: { result: Result; beforeSave: () => Promise<Result>; onLocate: (polygon: number[][]) => void }) {
  const [conflicts, setConflicts] = useState<Conflict[]>([]), [error, setError] = useState("");
  const [working, setWorking] = useState(false), [refresh, setRefresh] = useState(0);
  useEffect(() => { let valid = true; api<{ conflicts: Conflict[] }>(`/results/${result.id}/document-conflicts`).then(r => { if (valid) setConflicts(r.conflicts); }).catch(e => { if (valid) setError(String(e)); }); return () => { valid = false; }; }, [result.id, result.revision, refresh]);
  if (!conflicts.length && !error) return null;
  return <section className="geometry-panel" aria-label="页面内容核对"><strong>页面内容待核对</strong><p>对照原图检查遗漏或重叠文字，保存修正后再确认。修改内容后需要重新确认。</p>
    {conflicts.map(conflict => <div key={conflict.id}>
      {conflict.reason === "region_no_text" ? <p>此区域未识别出文字。请检查原图，确认没有遗漏；若有文字，请补录并定位，或重新 OCR。</p> : <><p>原生：{conflict.native.map(u => u.text).join(" ")}</p><p>OCR：{conflict.ocr.text}</p></>}
      {conflict.ocr.polygon && <Button size="small" onClick={() => onLocate(conflict.ocr.polygon!)}>查看原图区域</Button>}
      <Button size="small" disabled={working || conflict.reviewed} onClick={() => { setWorking(true); setError(""); void beforeSave().then(r => api(`/results/${r.id}/document-conflicts/${conflict.id}`, "POST", { revision: r.revision })).then(() => setRefresh(n => n+1)).catch(e => setError(String(e))).finally(() => setWorking(false)); }}>{conflict.reviewed ? "当前内容已核对" : "已核对当前保存内容"}</Button></div>)}
    {error && <p role="alert">{error}</p>}
  </section>;
}
