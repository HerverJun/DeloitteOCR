import type { AgentEvent, AgentRef, AuditTask } from './api'
import { projectConversation, terminalJob, type ConversationProjection, type JobProgress } from './conversationProjection'

export type WorkbenchJob = JobProgress & { observed: boolean }
export type WorkbenchBlocker = { key: string; title: string; detail?: string; reference?: AgentRef }
export type WorkbenchFile = { name: string; downloadUrl: string }
export type WorkbenchBundle = { id: string; title: string; files: WorkbenchFile[] }
export type WorkbenchDeliveryCheck = { phase: string; staged: boolean; message?: string; checks: { name: string; status: string; message?: string }[] }
export type WorkbenchView = {
  phase: string; currentJobs: WorkbenchJob[]; finishedJobs: WorkbenchJob[]; blockers: WorkbenchBlocker[]
  deliveryCheck?: WorkbenchDeliveryCheck; deliveryRefs: AgentRef[]; bundles: WorkbenchBundle[]; limitations: string[]; draftDelivery: boolean; availableResults: boolean
}

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
const text = (value: unknown) => typeof value === 'string' && value.trim() ? value : undefined
const jobIdentity = (ref: AgentRef) => [ref.workspace_id, ref.application, ref.parent_id, ref.object_id].join(':')
const completedJob = terminalJob
const references = (value: unknown): AgentRef[] => Array.isArray(value) ? value.filter((ref): ref is AgentRef => {
  const item = record(ref)
  return ['asset', 'asset_set', 'table', 'result', 'artifact', 'job', 'evidence'].includes(String(item.kind)) && typeof item.workspace_id === 'string' && typeof item.object_id === 'string' && ['string', 'number'].includes(typeof item.revision)
}) : []
const uniqueRefs = (refs: AgentRef[]) => [...new Map(refs.map(ref => [[ref.workspace_id, ref.application, ref.kind, ref.parent_id, ref.object_id, ref.revision].join(':'), ref])).values()]
const checkLabels: Record<string, string> = { native_job_status: '原生作业状态', csv_readback: 'CSV 完整读回', row_count: '行数', column_trimmed: '去除首尾空格', column_text_equals: '字段文本', column_decimal_places: '小数位数', column_prefix: '字段前缀', required_columns: '必要字段', column_names: '字段名称', source_references: '来源引用', artifact_readable: '成果可读取', artifact_exists: '成果存在', whitespace_trimmed: '去除首尾空格', leading_zeros: '前导零', amount_text: '金额文本', source_fields: '来源字段' }
const safeDownload = (value: unknown) => typeof value === 'string' && (/^\/(?!\/)/.test(value) || /^https?:\/\//i.test(value)) ? value : undefined

/** Read-only presentation of persisted task facts and authoritative SSE receipts. */
export function createWorkbenchView(task?: AuditTask, events: AgentEvent[] = [], existingProjection?: ConversationProjection): WorkbenchView {
  const projection = existingProjection || projectConversation(events)
  const ordered = [...projection.events].sort((a, b) => a.seq - b.seq)
  const runId = task?.runs?.[0]?.id || [...ordered].reverse().find(event => event.run_id)?.run_id
  const activity = runId ? projection.activities[runId] : undefined
  const phase = activity?.phase || task?.status || 'draft'
  const runEvents = ordered.filter(event => (!runId || event.run_id === runId) && !(typeof event.payload.generation === 'number' && event.payload.generation < (projection.generations[event.run_id || 'session'] || 0)))
  const latest = (kind: string) => [...runEvents].reverse().find(event => event.kind === kind)
  const jobs = new Map<string, WorkbenchJob>()
  for (const item of projection.items) if (item.type === 'activity') for (const job of item.jobs) {
    const key = jobIdentity(job.jobRef), previous = jobs.get(key)
    if (!previous || previous.seq < job.seq) jobs.set(key, { ...job, observed: true })
  }
  for (const jobRef of task?.job_refs || []) if (!jobs.has(jobIdentity(jobRef))) jobs.set(jobIdentity(jobRef), {
    id: jobIdentity(jobRef), jobRef, status: '', stage: '', seq: -1, observed: false, observation: projection.jobVerification === 'unavailable' ? 'unavailable' : undefined,
  })
  const allJobs = [...jobs.values()].sort((a, b) => b.seq - a.seq)
  const currentJobs = allJobs.filter(job => !completedJob(job.status) || job.observation === 'unavailable' || job.needsResume)
  const finishedJobs = allJobs.filter(job => !currentJobs.includes(job))
  const blockers: WorkbenchBlocker[] = []
  if (['waiting_user', 'needs_user'].includes(phase)) {
    const question = latest('question')
    blockers.push({ key: 'input', title: '等待你的补充', detail: text(question?.payload.question) || text(activity?.payload.reason) })
  } else if (['paused', 'failed', 'needs_verification', 'blocked'].includes(phase)) {
    const title = ({ paused: '任务已暂停', failed: '任务未完成', needs_verification: '操作结果待核实', blocked: '任务需要补充' } as Record<string, string>)[phase]
    const reason = text(activity?.payload.reason) || text(task?.runs?.[0]?.error) || text(latest('error')?.payload.message)
    blockers.push({ key: 'run', title, detail: reason })
  }
  for (const job of currentJobs) if (job.needsResume || job.observation === 'unavailable') blockers.push({
    key: `job:${job.id}`, title: job.needsResume ? '原生作业需要恢复' : '状态暂不可核实',
    detail: job.needsResume ? job.resumeMessage : undefined, reference: job.jobRef,
  })
  const checkEvent = latest('delivery_check')
  const payload = checkEvent?.payload
  const deliveryCheck = payload ? {
    phase: String(payload.phase || ''), staged: payload.phase === 'staged' || payload.phase === 'passed' && payload.finalized === false,
    message: text(payload.message), checks: (Array.isArray(payload.checks) ? payload.checks : []).map(value => {
      const check = record(value), name = String(check.name || '检查项')
      return { name: checkLabels[name] || name, status: String(check.status || 'pending'), message: text(check.message) }
    }),
  } : undefined
  const bundles = new Map<string, WorkbenchBundle>(), limitations = new Set<string>(), deliveryRefs = [...(task?.artifact_refs || []), ...(task?.result_refs || [])]
  let draftDelivery = false
  let availableResults = false
  for (const event of ordered) if (event.kind === 'result_available') {
    const result = record(event.payload.result), data = record(result.data)
    availableResults = true
    deliveryRefs.push(...references(result.artifact_refs), ...references(result.result_refs))
    for (const value of Array.isArray(data.delivery) ? data.delivery : []) {
      const bundle = record(value), id = text(bundle.bundle_id)
      if (!id) continue
      const files = (Array.isArray(bundle.files) ? bundle.files : []).flatMap(value => {
        const file = record(value), name = text(file.name), downloadUrl = safeDownload(file.download_url)
        return name && downloadUrl ? [{ name, downloadUrl }] : []
      })
      bundles.set(id, { id, title: text(bundle.title) || '已完成成果', files })
    }
  }
  for (const item of projection.items) if (item.type === 'message' && item.delivery && item.status === 'confirmed') {
    const delivery = record(item.delivery.payload.delivery), data = record(delivery.data)
    draftDelivery = data.draft === true || data.review_state === 'unreviewed'
    deliveryRefs.push(...references(delivery.artifact_refs), ...references(delivery.result_refs), ...item.references)
    for (const value of Array.isArray(data.delivery) ? data.delivery : []) {
      const bundle = record(value), id = text(bundle.bundle_id)
      if (!id) continue
      const files = (Array.isArray(bundle.files) ? bundle.files : []).flatMap(value => {
        const file = record(value), name = text(file.name), downloadUrl = safeDownload(file.download_url)
        return name && downloadUrl ? [{ name, downloadUrl }] : []
      })
      bundles.set(id, { id, title: text(bundle.title) || '成果文件', files })
    }
    for (const value of Array.isArray(data.limitations) ? data.limitations : []) if (text(value)) limitations.add(String(value))
  }
  return { phase, currentJobs, finishedJobs, blockers, deliveryCheck, deliveryRefs: uniqueRefs(deliveryRefs), bundles: [...bundles.values()], limitations: [...limitations], draftDelivery, availableResults }
}
