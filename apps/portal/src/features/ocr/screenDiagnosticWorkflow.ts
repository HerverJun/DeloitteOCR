export type DiagnosticAction = { action: string; label: string; enabled: boolean; reason?: string | null; capability_available?: boolean | null; budget_seconds?: number }
export type Diagnostic = { id: string; code: string; severity: string; reason: string; location: { level: 'cell' | 'table' | 'image'; table?: number | null; row?: number; column?: number; box?: number[] | null; basis?: string; range_semantics?: string }; evidence: Record<string, unknown>[]; applicable_actions: DiagnosticAction[] }
export type Diagnostics = { items: Diagnostic[]; groups?: unknown[]; summary: { issues: number; groups: number; cell: number; table: number; image: number }; budget: { remaining_seconds: number | null }; result_id?: string; result_revision?: number; input_version_id?: string }
export type CandidateComparison = { result_id: string; result_revision: number; input_version_id: string; current_id: string; alternative_id: string; recommended_candidate_id: string; diagnostics: Diagnostics; conflicts: { code: string; value?: string; alternative?: string; table?: number; row?: number; column?: number; message?: string }[]; selection_applied: false; values_modified: false }

export function currentResultRevision(item?: { result_revision?: number; current_result_revision?: number; result?: { revision?: number } }) {
  const revision = item?.result?.revision ?? item?.current_result_revision ?? item?.result_revision
  return Number.isInteger(revision) && Number(revision) >= 0 ? Number(revision) : undefined
}

export function reliableDiagnosticBox(item?: Diagnostic) {
  const location = item?.location, box = location?.box
  if (!location || !box || box.length !== 4 || box.some(value => !Number.isFinite(value)) || box[0] < 0 || box[1] < 0 || box[2] <= box[0] || box[3] <= box[1]) return null
  if (location.level === 'cell' && location.basis === 'observed_full_cell_geometry' && location.range_semantics === 'full_cell' && [location.table, location.row, location.column].every(value => Number.isInteger(value) && Number(value) >= 0)) return box
  if (location.level === 'table' && location.basis === 'observed_table_region') return box
  return null
}
export function diagnosticRange(item: Diagnostic) {
  const loc = item.location
  if (loc.level === 'cell' && reliableDiagnosticBox(item)) return `表 ${Number(loc.table) + 1} · 行 ${Number(loc.row) + 1} · 列 ${Number(loc.column) + 1}（可靠单格）`
  if (loc.level === 'table') return `${Number.isInteger(loc.table) ? `表 ${Number(loc.table) + 1}` : '业务表区'} · 无可靠单格定位`
  return '全图 · 无可靠单格定位'
}
export function diagnosticActionReason(reason?: string | null) {
  return reason === 'remaining_budget_insufficient' ? '剩余预算不足' : reason === 'capability_not_verified' ? '能力尚未核实' : reason || '当前不可用'
}
