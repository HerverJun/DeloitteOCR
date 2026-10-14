import { HttpError, newRequestId, post, request, type LocalActor } from '../batches/api'
import type {
  AuditFindingCreate, AuditFindingWrite, AuditProcedureCreate, AuditProcedureWrite,
  AuditProjectCreate, AuditProjectWrite, AuditRecordCreate, AuditRequestStatus,
  AuditWriteReceipt, ReviewBundle, WorkpaperBundle, WorkpaperRequest,
} from '../../../../../packages/contracts/types.generated'

export type {
  AuditProject, AuditProjectFields, AuditProjectPage, AuditProcedure, AuditProcedureFields,
  AuditProcedurePage, AuditFinding, AuditFindingFields, AuditFindingPage, AuditEvidenceRef,
  AuditEvidenceSelection, AuditRecord, AuditWriteReceipt, WorkpaperBundle, ReviewBundle,
} from '../../../../../packages/contracts/types.generated'

export type AuditPayload = AuditProjectCreate | AuditProjectWrite | AuditProcedureCreate |
  AuditProcedureWrite | AuditFindingCreate | AuditFindingWrite | AuditRecordCreate | WorkpaperRequest
export type AuditReceipt = AuditWriteReceipt | WorkpaperBundle
export type PendingAudit = {
  workspace_id: string; project_id?: string; path: string; label: string; payload: AuditPayload;
  status: 'uncertain' | 'conflict' | 'rejected'; reason?: string
}
const pendingKey = (workspaceId: string) => `clear-audit-request:${workspaceId}`

export function pendingAudit(workspaceId: string): PendingAudit | null {
  try {
    const value = JSON.parse(localStorage.getItem(pendingKey(workspaceId)) || 'null')
    const payload = value?.payload
    if (value?.workspace_id !== workspaceId || !['uncertain', 'conflict', 'rejected'].includes(value.status) ||
      typeof value.path !== 'string' || typeof value.label !== 'string' ||
      !payload || !/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(payload.request_id) || payload.schema_version !== '1.0' ||
      payload.actor?.source !== 'local' || typeof payload.actor.display_name !== 'string') return null
    const projectPath = value.project_id && `/audit-projects/${encodeURIComponent(value.project_id)}`
    const createPath = `/workspaces/${encodeURIComponent(workspaceId)}/audit-projects`
    if (value.path !== createPath && (!projectPath || !(value.path === projectPath || value.path.startsWith(projectPath + '/')))) return null
    return value
  } catch { return null }
}
export function persistAudit(workspaceId: string, value: PendingAudit | null) {
  if (value) localStorage.setItem(pendingKey(workspaceId), JSON.stringify(value))
  else localStorage.removeItem(pendingKey(workspaceId))
}
export function auditCommand(actor: LocalActor) {
  return { schema_version: '1.0' as const, audit_version: '1.0' as const,
    request_id: newRequestId(), actor: { ...actor, display_name: actor.display_name.trim() } }
}
export const auditProjectPath = (projectId: string) => `/audit-projects/${encodeURIComponent(projectId)}`
export const replayAudit = (pending: PendingAudit) => post<AuditReceipt>(pending.path, pending.payload)
export const auditRequestStatus = (pending: PendingAudit) => request<AuditRequestStatus>(
  `${auditProjectPath(pending.project_id!)}/requests/${encodeURIComponent(pending.payload.request_id)}`)
export function pendingFailure(error: Error, pending: PendingAudit): PendingAudit {
  const status = error instanceof HttpError ? error.status : 0
  return { ...pending, status: status === 409 ? 'conflict' : [400, 403, 404, 422].includes(status) ? 'rejected' : 'uncertain', reason: error.message }
}

type ArtifactSummary = { bundle_id: string; kind: string; project_id?: string }
export async function auditArtifacts(workspaceId: string, signal: AbortSignal) {
  const result: (ReviewBundle | WorkpaperBundle)[] = []
  const base = `/workspaces/${encodeURIComponent(workspaceId)}/artifacts`
  let offset: number | null = 0
  const selected: ArtifactSummary[] = []
  do {
    const page: { items: ArtifactSummary[]; next_offset: number | null } = await request(`${base}?offset=${offset}&limit=100`, { signal })
    selected.push(...page.items.filter(item => item.kind === 'batch_review' || item.kind === 'audit_workpaper'))
    if (page.next_offset !== null && page.next_offset <= offset) throw new Error('成果分页无效，请刷新')
    offset = page.next_offset
  } while (offset !== null)
  // Detail reads resolve current/outdated status instead of trusting a historical list entry.
  for (const item of selected) result.push(await request<ReviewBundle | WorkpaperBundle>(`${base}/${encodeURIComponent(item.bundle_id)}`, { signal }))
  return result
}
