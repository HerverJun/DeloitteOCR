import { Button } from "@fluentui/react-components";

export type ScreenMetadata = { kind: string; text: string; source?: string; table?: number | null; box?: number[]; alternative_text?: string; alternative_source?: string; human_confirmed?: boolean; review_required?: boolean; transcription_evidence?: { primary_text?: string; auxiliary_text?: string; primary_source?: Record<string, unknown>; auxiliary_source?: Record<string, unknown> } };
export type ScreenMetadataEdit = { item_index: number; before: string; after: string; choice: "manual" | "primary" | "auxiliary" };

export function ScreenBusinessMetadata({ items, edits, busy, change }: { items: ScreenMetadata[]; edits: Record<number, ScreenMetadataEdit>; busy: boolean; change: (edit: ScreenMetadataEdit) => void }) {
  return <section aria-label="业务说明复核"><h4>标题、单位与表注</h4>
    <p className="screen-batch-small">保留两版识别原文与来源；选值或手改后保存到当前修订。识别一致不代表人工确认。</p>
    {!items.length && <p>没有已定位的业务说明，请结合原图检查遗漏。</p>}
    {items.map((part, index) => <div key={index} className="screen-issue">
      <strong>{({ title: "标题", unit: "单位", note: "表注", caption: "表题" } as Record<string, string>)[part.kind] || part.kind} {index + 1}{part.table != null ? ` · 表 ${part.table + 1}` : ""}</strong>
      <p>当前文字：{part.text}</p><p>来源：{part.source || "未确定"} · {part.human_confirmed ? "人工已确认" : part.review_required ? "待核对" : "识别候选，未逐项人工确认"}</p>
      {part.alternative_text !== undefined && <p>另一识别原文：{part.alternative_text}（{part.alternative_source || "来源未确定"}）</p>}
      {part.transcription_evidence && <details><summary>查看两版原文及识别来源</summary><p>主识别：{part.transcription_evidence.primary_text}</p><p>辅助识别：{part.transcription_evidence.auxiliary_text}</p><pre style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(part.transcription_evidence, null, 2)}</pre></details>}
      <div className="screen-batch-actions"><Button size="small" disabled={busy || part.transcription_evidence?.primary_text === undefined || !part.transcription_evidence.primary_source} onClick={() => change({ item_index: index, before: part.text, after: part.transcription_evidence!.primary_text!, choice: "primary" })}>选用主识别原文 {index + 1}</Button>
        <Button size="small" disabled={busy || part.transcription_evidence?.auxiliary_text === undefined} onClick={() => change({ item_index: index, before: part.text, after: part.transcription_evidence!.auxiliary_text!, choice: "auxiliary" })}>选用辅助识别原文 {index + 1}</Button></div>
      <label>复核值<textarea aria-label={`业务说明${index + 1}复核值`} disabled={busy} rows={3} maxLength={10000} value={edits[index]?.after ?? part.text} onChange={event => change({ item_index: index, before: part.text, after: event.target.value, choice: "manual" })} /></label>
    </div>)}
  </section>;
}
