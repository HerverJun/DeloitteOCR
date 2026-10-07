import { useState } from "react";
import { Button } from "@fluentui/react-components";
import type { Table } from "./types";
import { screenCandidateCell, screenIssueTitle, type ScreenCandidate, type ScreenIssue } from "./screenBatchWorkflow";

const engineName = (engine: string) => ({ glm: "GLM-OCR", hunyuan: "HunyuanOCR", paddlevl: "PaddleOCR-VL" } as Record<string,string>)[engine] || engine;

/** Preview complete image candidates, never individual crop task results. */
export function ScreenCandidatePreview({ candidates, inputVersionId, currentTables, tableIndex, row, column,
  currentValue, selectedCandidateId, issues, busy, hasDraft, canAdopt, onFill, onAdopt }: {
  candidates: ScreenCandidate[]; inputVersionId: string; currentTables: Table[]; tableIndex: number; row: number; column: number;
  currentValue: string; issues: ScreenIssue[]; busy: boolean; hasDraft: boolean; canAdopt: boolean;
  selectedCandidateId?: string;
  onFill: (value: string) => void; onAdopt: (candidateId: string) => void;
}) {
  const [candidateId, setCandidateId] = useState(() =>
    candidates.find(candidate => candidate.id === selectedCandidateId)?.id
    || candidates.find(candidate => candidate.raw.tables.length > 0)?.id || candidates[0]?.id || "");
  const [previewTable, setPreviewTable] = useState(0), [offset, setOffset] = useState(0);
  const candidate = candidates.find(value => value.id === candidateId);
  if (!candidate) return null;
  const sameImage = candidate.raw.project_image_version === inputVersionId;
  const correspondence = sameImage ? screenCandidateCell(currentTables, candidate.raw.tables, tableIndex, row, column)
    : { cell: undefined, reason: "候选的图片版本不一致，请刷新后重新核对。" };
  const table = candidate.raw.tables[previewTable];
  const end = Math.min(offset + 20, table?.rows || 0);
  const reasons = issues.filter(issue => issue.candidate_id === candidate.id);
  return <details className="screen-candidate-preview" open>
    <summary>对比识别候选（{candidates.length} 个整图结果）</summary>
    <div className="screen-candidate-list" aria-label="本图所有识别候选">{candidates.map(value => <button key={value.id}
      type="button" aria-pressed={candidateId === value.id} onClick={() => { setCandidateId(value.id); setPreviewTable(0); setOffset(0); }}>
      <strong>{engineName(value.engine)}</strong><span>{value.raw.tables.length} 张表 · {value.raw.tables.map(table => `${table.rows} 行 × ${table.columns} 列`).join("；") || "无结构化表格"}</span>
    </button>)}</div>
    <p className="screen-batch-small">预览切换会保留当前修改。采用整图候选后，该图片重新进入待核对；已保存结果按版本检查。</p>
    <div className="screen-candidate-cell" aria-label="当前格候选对比">
      <dl><div><dt>当前内容{hasDraft ? "（含待保存修改）" : ""}</dt><dd>{currentValue || "（空白）"}</dd></div>
        <div><dt>{engineName(candidate.engine)} 对应格</dt><dd>{correspondence.cell ? correspondence.cell.text || "（空白）" : "无法建立可靠对应"}</dd></div></dl>
      <p className="screen-batch-small">{correspondence.reason}</p>
      {correspondence.cell && <Button size="small" disabled={busy || correspondence.cell.text === currentValue} onClick={() => onFill(correspondence.cell!.text)}>填入当前格草稿，等待核对保存</Button>}
    </div>
    {reasons.length > 0 && <details className="screen-candidate-reasons"><summary>此候选相关疑点（{reasons.length}）</summary><ul>{reasons.map((issue,index) => <li key={index}>{screenIssueTitle(issue)}{Number.isInteger(issue.table) ? ` · 表 ${issue.table!+1}` : ""}{Number.isInteger(issue.row) ? `，第 ${issue.row!+1} 行` : ""}</li>)}</ul></details>}
    <div className="screen-candidate-meta"><span>整图合并结果 · {candidate.raw.screen?.sources?.length || 1} 个识别输入</span>
      {candidate.raw.elapsed_seconds !== undefined && <span>识别 {candidate.raw.elapsed_seconds.toFixed(1)} 秒</span>}
      {candidate.raw.truncated && <span>输出曾截断，需核对完整性</span>}</div>
    {table ? <>
      <label>候选表格<select aria-label="候选预览表格" value={previewTable} onChange={event => { setPreviewTable(Number(event.target.value)); setOffset(0); }}>{candidate.raw.tables.map((table,index) => <option key={index} value={index}>表 {index+1} · {table.rows} 行 × {table.columns} 列{table.caption ? ` · ${table.caption}` : ""}</option>)}</select></label>
      <div className="screen-candidate-table"><table aria-label={`${engineName(candidate.engine)} 表 ${previewTable+1} 候选预览`}>
        {table.caption && <caption>{table.caption}</caption>}<tbody>{Array.from({length:end-offset},(_,index) => offset+index).map(r => <tr key={r}>
          {table.cells.filter(cell => cell.row === r || r === offset && cell.row < offset && cell.row + cell.row_span > offset)
            .sort((left,right) => left.column-right.column).map(cell => <td key={`${cell.row}:${cell.column}`}
              rowSpan={Math.min(cell.row+cell.row_span-r,end-r)} colSpan={cell.column_span} title={`第 ${cell.row+1} 行，第 ${cell.column+1} 列${cell.row<offset ? "（合并格续页）" : ""}`}>{cell.text}</td>)}
        </tr>)}</tbody></table></div>
      <div className="screen-batch-pages"><Button size="small" disabled={offset===0} onClick={() => setOffset(Math.max(0,offset-20))}>候选上一页</Button>
        <span>第 {offset+1}–{end} 行 / 全部 {table.rows} 行</span><Button size="small" disabled={end>=table.rows} onClick={() => setOffset(end)}>候选下一页</Button></div>
    </> : <p>此候选未提供表格。</p>}
    {candidate.raw.warnings?.length ? <details><summary>模型输出提示（{candidate.raw.warnings.length}）</summary><ul>{candidate.raw.warnings.map((warning,index) => <li key={index}>{warning.message || warning.code}</li>)}</ul></details> : null}
    <Button disabled={busy || hasDraft || !canAdopt || !sameImage || !candidate.raw.tables.length} onClick={() => onAdopt(candidate.id)}>采用此整图候选，继续核对</Button>
    {hasDraft && <p className="screen-batch-small">采用前请先保存或清除当前单元格修改。</p>}
  </details>;
}
