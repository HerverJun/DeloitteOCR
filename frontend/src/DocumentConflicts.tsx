import { useEffect, useState } from "react";
import { Button } from "@fluentui/react-components";
import { api } from "./api";
import type { Result } from "./types";
type Conflict = { id: string; reviewed: boolean; native: { text: string }[]; ocr: { text: string } };
export function DocumentConflicts({ result, beforeSave }: { result: Result; beforeSave: () => Promise<Result> }) {
  const [conflicts, setConflicts] = useState<Conflict[]>([]), [error, setError] = useState("");
  const [working, setWorking] = useState(false), [refresh, setRefresh] = useState(0);
  useEffect(() => { let valid = true; api<{ conflicts: Conflict[] }>(`/results/${result.id}/document-conflicts`).then(r => { if (valid) setConflicts(r.conflicts); }).catch(e => { if (valid) setError(String(e)); }); return () => { valid = false; }; }, [result.id, result.revision, refresh]);
  if (!conflicts.length && !error) return null;
  return <section className="geometry-panel" aria-label="原生与OCR重叠核对"><strong>原生内容与 OCR 重叠</strong><p>对照原图和两份来源，在文字编辑区删除重复内容或修正文字，再确认当前保存内容。修改内容后需要重新确认。</p>
    {conflicts.map(conflict => <div key={conflict.id}><p>原生：{conflict.native.map(u => u.text).join(" ")}</p><p>OCR：{conflict.ocr.text}</p>
      <Button size="small" disabled={working || conflict.reviewed} onClick={() => { setWorking(true); setError(""); void beforeSave().then(r => api(`/results/${r.id}/document-conflicts/${conflict.id}`, "POST", { revision: r.revision })).then(() => setRefresh(n => n+1)).catch(e => setError(String(e))).finally(() => setWorking(false)); }}>{conflict.reviewed ? "当前内容已核对" : "已核对当前保存内容"}</Button></div>)}
    {error && <p role="alert">{error}</p>}
  </section>;
}
