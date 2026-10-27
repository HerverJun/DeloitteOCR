import { randomUUID } from '../../clear-ui/requestId'
import type { BatchCoverage, BatchDelivery, BatchDisposition, BatchHistoryPage, BatchNativeRevisionPage, BatchOperation, LocalActor, NativeAdoption, ReviewArtifactRequest, ReviewBundle } from '../../../../../packages/contracts/types.generated'
export type { BatchCoverage, BatchDisposition, BatchHistoryPage, BatchNativeRevisionPage, BatchOperation, LocalActor, NativeAdoption, ReviewArtifactRequest, ReviewBundle }
export const API = '/api/platform/v1'
export type Scenario = 'data_compare' | 'classification' | 'ocr'
export type Template = { template_id: string; version: number; name: string; scenario: Scenario; config: Record<string, unknown>; config_hash: string }
export type Asset = { asset_id: string; original_name: string; media_type: string; revision: number; blob_hash: string }
export type Issue = { item_id: string; category: string; title: string; detail: string; source: Record<string, unknown> | null; raw_output: unknown; disposition: 'pending' | 'confirmed' | 'deferred'; note: string; adoption?: Record<string, unknown> | null }
export type IssuePage = { revision: number; items: Issue[]; total: number; coverage: BatchCoverage; summary: Summary }
export type Summary = { total: number; processed: number; succeeded: number; failed: number; pending: number; confirmed: number; deferred: number; coverage: Record<string, number>; execution_complete: boolean; review_complete: boolean; pending_operations?: number; pending_operation?: { operation_id: string; request_id: string; state: string } | null; review_scope?: string; native_review_qualification?: string }
export type Artifact = { bundle_id: string; title: string; kind?: string; files: { name: string; availability: string }[] }
export type Batch = { revision: number; coverage: BatchCoverage; delivery: BatchDelivery; batch_id: string; workspace_id: string; template: Template; template_id: string; template_version: number; status: string; stage: string; summary: Summary; issues: Issue[]; receipts: Record<string, unknown>; artifact_state: string; artifacts: Artifact[]; capabilities: string[]; created_at: string; updated_at: string }
export type BatchInput = { role: string; asset_id: string; asset_revision: number; blob_hash: string }
export type RunRequest = { schema_version: '1.0'; request_key: string; workspace_id: string; template_id: string; template_version: number; inputs: BatchInput[] }
export type Preflight = { ready?: boolean; ok?: boolean; issues: { title?: string; detail?: string; message?: string; code?: string }[] }
export const scenarioNames: Record<Scenario, string> = { data_compare: '两表清洗与比对', classification: '图像批量分类', ocr: '文档识别与提取' }
export const stateNames: Record<string, string> = { queued: '排队', running: '执行中', waiting_input: '等待补充输入', waiting_resource: '等待资源', partial: '部分完成', succeeded: '执行成功', failed: '执行失败', cancelled: '已取消', cancelling: '等待取消确认', needs_verification: '待核实', pending: '待处理', confirmed: '人工确认', deferred: '暂缓', not_generated: '尚未生成', generated: '已生成', collected: '归集成功', collection_failed: '归集失败' }
export const stageNames: Record<string, string> = { preflight: '预检', session: '登记数据作业', upload_left: '登记左表来源', upload_right: '登记右表来源', import_left: '导入左表', import_right: '导入右表', import: '导入资料', replay: '重放清洗规则', compare: '比对', results: '保存比对结果', export: '生成固定版本成果', generated: '固定成果已生成', collect: '归集成果', classification: '图像分类', ocr: '文档识别', completed: '阶段结束', done: '阶段结束', pending: '等待执行', cancelled: '已取消', native_inputs: '登记原生输入', native_import: '导入原生项目', native_submission: '提交原生任务', native_dispatch: '派发原生任务', native_progress: '等待原生任务完成', native_snapshot: '保存固定结果', native_collection: '归集原生成果' }
export function stageLabel(value: string) { return stageNames[value] || (/^clean_(left|right)_\d+$/.test(value) ? `${value.includes('_left_') ? '左表' : '右表'}清洗 · 第 ${Number(value.split('_')[2]) + 1} 步` : value) }
export const terminal = ['succeeded', 'partial', 'failed', 'cancelled', 'waiting_input', 'needs_verification']

const pendingKey = (workspaceId: string) => `clear-batch-request:${workspaceId}`
export function pendingRequest(workspaceId: string): RunRequest | null {
  const saved = localStorage.getItem(pendingKey(workspaceId))
  if (!saved) return null
  try {
    const value = JSON.parse(saved) as RunRequest
    return value.workspace_id === workspaceId && typeof value.request_key === 'string' && Array.isArray(value.inputs) ? value : null
  } catch { return null }
}
export function persistRequest(workspaceId: string, value: RunRequest | null) {
  if (value) localStorage.setItem(pendingKey(workspaceId), JSON.stringify(value))
  else localStorage.removeItem(pendingKey(workspaceId))
}

export function newRequestId() { return 'r' + randomUUID().replaceAll('-', '') }
export function localReviewer(): LocalActor {
  try {
    const value = JSON.parse(localStorage.getItem('clear-local-reviewer') || 'null')
    if (value?.source === 'local' && typeof value.id === 'string' && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(value.id) && typeof value.display_name === 'string') return value
  } catch { /* The user can supply a fresh explicit display name. */ }
  const value: LocalActor = { id: 'u' + randomUUID().replaceAll('-', ''), display_name: '', source: 'local' }
  localStorage.setItem('clear-local-reviewer', JSON.stringify(value))
  return value
}
export type FixedRequest<T> = { payload: T; status: 'uncertain' | 'conflict' }
export function fixedRequest<T>(kind: 'disposition' | 'review', batchId: string): FixedRequest<T> | null {
  try {
    const value = JSON.parse(localStorage.getItem(`clear-batch-${kind}:${batchId}`) || 'null')
    const payload = value?.payload
    if (!['uncertain', 'conflict'].includes(value?.status) || !payload || typeof payload.request_id !== 'string' ||
      !/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(payload.request_id) || !Number.isInteger(payload.expected_revision) || payload.expected_revision < 1 ||
      payload.actor?.source !== 'local' || typeof payload.actor.id !== 'string' || typeof payload.actor.display_name !== 'string') return null
    if (kind === 'disposition' && (!Array.isArray(payload.item_ids) || !payload.item_ids.length || !['confirm', 'defer', 'reopen', 'adopt'].includes(payload.action))) return null
    if (kind === 'review' && !['intermediate', 'final'].includes(payload.kind)) return null
    return value
  }
  catch { return null }
}
export function persistFixed<T>(kind: 'disposition' | 'review', batchId: string, value: FixedRequest<T> | null) {
  const key = `clear-batch-${kind}:${batchId}`
  if (value) localStorage.setItem(key, JSON.stringify(value))
  else localStorage.removeItem(key)
}

export class HttpError extends Error {
  constructor(message: string, readonly status: number) { super(message); this.name = 'HttpError' }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(API + path, init)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new HttpError(typeof body.detail === 'string' ? body.detail : `操作未完成（${response.status}）`, response.status)
  }
  return response.json() as Promise<T>
}
export function post<T>(path: string, body: unknown) {
  return request<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
}
export async function assets(workspaceId: string, signal: AbortSignal) {
  const result: Asset[] = []
  let offset: number | null = 0
  do {
    const page: { items: Asset[]; next_offset: number | null } = await request(`/workspaces/${encodeURIComponent(workspaceId)}/assets?offset=${offset}&limit=100`, { signal })
    result.push(...page.items)
    if (page.next_offset !== null && page.next_offset <= offset) throw new Error('资料列表分页无效，请刷新')
    offset = page.next_offset
  } while (offset !== null)
  return result
}
