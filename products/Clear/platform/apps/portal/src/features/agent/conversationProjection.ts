import type { AgentEvent, AgentRef } from './api'

export type MessageSegment = { type: 'message'; id: string; seq: number; runId: string | null; generation: number; created: string; role: 'user' | 'assistant'; content: string; source: string; status: 'preview' | 'generated' | 'confirmed' | 'interrupted'; revision: number; references: AgentRef[]; reason?: string; queued?: boolean; delivery?: AgentEvent; truncated?: boolean }
export type ToolStep = { id: string; callId: string; operationId?: string; tool: string; phase: string; status: string; summary: string; created: string; seq: number; jobRefs: AgentRef[]; events: AgentEvent[] }
export type JobProgress = { id: string; jobRef: AgentRef; status: string; stage: string; processed?: number; total?: number; counts?: Record<string, number>; sampledAt?: string; observation?: string; availability?: string; hasError?: boolean; needsResume?: boolean; resumeMessage?: string; seq: number; generation?: number; runId?: string | null }
export type ActivityGroup = { type: 'activity'; id: string; seq: number; runId: string | null; generation: number; created: string; tools: ToolStep[]; jobs: JobProgress[]; diagnostics: AgentEvent[] }
export type TimelineEvent = { type: 'event'; id: string; seq: number; event: AgentEvent }
export type ConversationItem = MessageSegment | ActivityGroup | TimelineEvent
export type RunActivity = { runId: string; generation: number; phase: string; created: string; seq: number; payload: Record<string, unknown> }
export type ConversationProjection = { items: ConversationItem[]; activities: Record<string, RunActivity>; generations: Record<string, number>; seen: Set<number>; events: AgentEvent[]; protocolIssue?: string; jobVerification?: 'verified' | 'unavailable'; replayCursor?: number }
export const createProjection = (): ConversationProjection => ({ items: [], activities: {}, generations: {}, seen: new Set(), events: [] })
const terminalPhases = new Set(['completed', 'failed', 'stopped', 'cancelled', 'manual', 'answered', 'succeeded', 'partial', 'incomplete'])
export const terminalActivity = (phase: string) => terminalPhases.has(phase)
const generationOf = (event: AgentEvent) => typeof event.payload.generation === 'number' ? event.payload.generation : 0
const references = (value: unknown): AgentRef[] => Array.isArray(value) ? value as AgentRef[] : []
export const jobKey = (ref: AgentRef) => [ref.workspace_id, ref.application, ref.parent_id, ref.object_id].join(':')
export const terminalJob = (status: string) => ['succeeded', 'success', 'completed', 'failed', 'error', 'cancelled', 'stopped', 'partial', 'incomplete'].includes(status)
type JobFact = Record<string, unknown> & { job_ref: AgentRef }
export type JobSnapshot = { jobs: JobFact[]; sampled_at: string }
export const validJobSnapshot = (value: unknown): value is JobSnapshot => {
  if (!value || typeof value !== 'object') return false
  const snapshot = value as JobSnapshot
  return typeof snapshot.sampled_at === 'string' && Array.isArray(snapshot.jobs) && snapshot.jobs.every(job => job && typeof job === 'object' && job.job_ref?.kind === 'job' && typeof job.job_ref.workspace_id === 'string' && typeof job.job_ref.object_id === 'string' && typeof job.status === 'string')
}
function jobProgress(p: JobFact, seq: number, sampledAt?: string, runId?: string | null): JobProgress {
  return { id: jobKey(p.job_ref), jobRef: p.job_ref, status: String(p.status || ''), stage: String(p.stage || ''), seq,
    runId: typeof p.run_id === 'string' ? p.run_id : runId,
    generation: typeof p.generation === 'number' ? p.generation : undefined,
    processed: typeof p.progress_done === 'number' ? p.progress_done : typeof p.processed === 'number' ? p.processed : undefined,
    total: typeof p.progress_total === 'number' ? p.progress_total : typeof p.total === 'number' ? p.total : undefined,
    counts: p.counts && typeof p.counts === 'object' ? p.counts as Record<string, number> : undefined,
    sampledAt: typeof p.sampled_at === 'string' ? p.sampled_at : sampledAt,
    observation: typeof p.observation === 'string' ? p.observation : undefined,
    availability: typeof p.availability === 'string' ? p.availability : undefined,
    hasError: typeof p.has_error === 'boolean' ? p.has_error : undefined, needsResume: p.needs_resume === true,
    resumeMessage: typeof p.resume_message === 'string' ? p.resume_message : undefined }
}
function mergeJob(old: JobProgress | undefined, value: JobProgress): JobProgress {
  if (!old) return value
  if (old.runId === value.runId && typeof old.generation === 'number' && typeof value.generation === 'number' && value.generation < old.generation) return old
  // A late queue/running observation never reverses a persisted terminal receipt.
  if (terminalJob(old.status) && !terminalJob(value.status)) return { ...old, seq: Math.max(old.seq, value.seq), jobRef: value.jobRef,
    ...(value.observation === 'unavailable' ? { observation: 'unavailable', availability: value.availability, hasError: value.hasError, sampledAt: value.sampledAt || old.sampledAt } : {}) }
  if (value.seq >= 0 && old.seq > value.seq) return old
  const oldTime = Date.parse(old.sampledAt || ''), nextTime = Date.parse(value.sampledAt || '')
  if (Number.isFinite(oldTime) && Number.isFinite(nextTime) && nextTime < oldTime) return old
  return { ...value, seq: Math.max(old.seq, value.seq) }
}
const latestJob = (jobs: JobProgress[]) => jobs.reduce<JobProgress | undefined>((previous, job) => !previous || job.seq > previous.seq || job.seq === previous.seq && Date.parse(job.sampledAt || '') > Date.parse(previous.sampledAt || '') ? job : previous, undefined)
/** Snapshot reads update the projection without inventing events or advancing the SSE cursor. */
export function applyJobSnapshot(previous: ConversationProjection, snapshot?: JobSnapshot): ConversationProjection {
  const items = previous.items.map(item => item.type === 'activity' ? { ...item, jobs: item.jobs.map(job => ({ ...job, ...(snapshot ? {} : { observation: 'unavailable' }) })) } : item)
  const result: ConversationProjection = { ...previous, items, jobVerification: snapshot ? 'verified' : 'unavailable' }
  if (!snapshot) return result
  const present = new Set(snapshot.jobs.map(job => jobKey(job.job_ref)))
  for (const item of items) if (item.type === 'activity') item.jobs = item.jobs.map(job => present.has(job.id) ? job : { ...job, observation: 'unavailable' })
  for (const fact of snapshot.jobs) {
    const id = jobKey(fact.job_ref), matching = items.filter((item): item is ActivityGroup => item.type === 'activity' && item.jobs.some(job => job.id === id))
    const old = latestJob(matching.flatMap(group => group.jobs).filter(job => job.id === id))
    if (typeof fact.run_id === 'string' && typeof fact.generation === 'number' && fact.generation < (result.generations[fact.run_id] || 0)) continue
    const value = mergeJob(old, jobProgress(fact, typeof fact.seq === 'number' ? fact.seq : -1, snapshot.sampled_at))
    if (matching.length) for (const group of matching) group.jobs = group.jobs.map(job => job.id === id ? value : job)
    else {
      const runId = typeof fact.run_id === 'string' ? fact.run_id : null, groupId = `activity:${runId || 'session'}`
      let group = items.find((item): item is ActivityGroup => item.type === 'activity' && item.id === groupId)
      if (!group) { group = { type: 'activity', id: groupId, runId, generation: value.generation || 0, seq: value.seq, created: snapshot.sampled_at, tools: [], jobs: [], diagnostics: [] }; items.push(group) }
      group.jobs.push(value)
    }
  }
  return result
}

export function applyConversationEvents(previous: ConversationProjection, incoming: AgentEvent[]): ConversationProjection {
  const result: ConversationProjection = { ...previous, items: [...previous.items], activities: { ...previous.activities }, generations: { ...previous.generations }, seen: new Set(previous.seen), events: [...previous.events] }
  const indices = new Map(result.items.map((item, index) => [item.id, index]))
  const put = (item: ConversationItem) => { const index = indices.get(item.id); if (index === undefined) { indices.set(item.id, result.items.length); result.items.push(item) } else result.items[index] = item }
  const group = (event: AgentEvent): ActivityGroup => {
    const id = `activity:${event.run_id || 'session'}`, index = indices.get(id)
    const existing = index === undefined ? undefined : result.items[index]
    return existing?.type === 'activity' ? { ...existing, tools: [...existing.tools], jobs: [...existing.jobs], diagnostics: [...existing.diagnostics] } : { type: 'activity', id, seq: event.seq, runId: event.run_id, generation: generationOf(event), created: event.created, tools: [], jobs: [], diagnostics: [] }
  }
  const identity = (event: AgentEvent) => event.payload.display_id
    ? `message:${event.run_id || 'session'}:display:${String(event.payload.display_id)}`
    : `message:${event.run_id || 'session'}:${generationOf(event)}:${String(event.payload.message_id || `legacy-${event.seq}`)}`
  const message = (event: AgentEvent): MessageSegment => {
    const id = identity(event), index = indices.get(id), existing = index === undefined ? undefined : result.items[index]
    return existing?.type === 'message' ? { ...existing } : { type: 'message', id, seq: event.seq, runId: event.run_id, generation: generationOf(event), created: event.created, role: 'assistant', content: '', source: String(event.payload.source || 'text'), status: 'preview', revision: 0, references: [] }
  }
  const interrupt = (runId: string | null, reason: string) => {
    for (const item of result.items) if (item.type === 'message' && item.runId === runId && ['preview', 'generated'].includes(item.status)) put({ ...item, status: 'interrupted', reason, references: [] })
  }
  for (const event of [...incoming].sort((a, b) => a.seq - b.seq)) {
    if (result.seen.has(event.seq)) continue
    result.seen.add(event.seq); result.events.push(event)
    const p = event.payload, generation = generationOf(event), run = event.run_id || 'session'
    const stale = generation < (result.generations[run] || 0)
    if (typeof p.generation === 'number') result.generations[run] = Math.max(result.generations[run] || 0, generation)
    if (event.kind === 'run_resumed') {
      if (!stale) result.activities[run] = { runId: run, generation, phase: 'preparing_request', created: event.created, seq: event.seq, payload: p }
      continue
    }
    if (event.kind === 'result_available') {
      const available = p.result && typeof p.result === 'object' ? p.result as Record<string, unknown> : undefined
      if (!available) continue
      const delivered = { ...event, payload: { ...p, status: 'available', delivery: available, content: '已完成成果可读取和下载。' } }
      const item = message(delivered)
      item.status = 'confirmed'; item.source = 'available_result'; item.content = String(delivered.payload.content); item.delivery = delivered
      item.references = [...references(available.result_refs), ...references(available.artifact_refs)]
      put(item); continue
    }
    if (event.kind === 'agent_activity') {
      if (stale || p.version !== 1 || typeof p.phase !== 'string') continue
      const current = result.activities[run]
      if (current && current.generation === generation && terminalActivity(current.phase)) continue
      result.activities[run] = { runId: run, generation, phase: p.phase, created: event.created, seq: event.seq, payload: p }
      if (terminalActivity(p.phase) && !['completed', 'answered', 'succeeded', 'partial'].includes(p.phase)) interrupt(event.run_id, String(p.reason || '本轮生成已结束'))
      continue
    }
    if (event.kind === 'response_preview' || event.kind === 'response_state' || event.kind === 'model_delta') {
      if (stale || event.kind !== 'model_delta' && p.version !== 1) continue
      const item = message(event)
      if (event.kind === 'response_preview' || event.kind === 'model_delta') {
        if (['confirmed', 'interrupted'].includes(item.status) || terminalActivity(result.activities[run]?.phase || '')) continue
        const revision = event.kind === 'model_delta' ? event.seq : Number(p.revision)
        if (!Number.isInteger(revision) || revision <= item.revision || typeof p.content !== 'string') continue
        const mode = event.kind === 'model_delta' ? 'snapshot' : p.mode
        if (mode === 'append') {
          if (revision !== item.revision + 1 || p.offset !== Array.from(item.content).length) { result.protocolIssue = '文字片段尚不连续，正在核实订阅；正式结果保留。'; continue }
          item.content += p.content
        } else if (mode === 'snapshot' || mode === 'replace') item.content = p.content
        else continue
        item.revision = revision; item.status = 'preview'; item.references = []; item.source = String(p.source || item.source); item.truncated = p.truncated === true
        if (mode === 'snapshot' || mode === 'replace') result.protocolIssue = undefined
      } else {
        const revision = Number(p.revision)
        if (!Number.isInteger(revision) || revision < item.revision || !['generated', 'confirmed', 'interrupted'].includes(String(p.status))) continue
        if (item.status === 'confirmed' && p.status !== 'confirmed' || item.status === 'interrupted' && p.status === 'generated') continue
        if (typeof p.content === 'string' && (p.content.trim() || !item.content)) item.content = p.content
        item.generation = generation; item.revision = revision; item.status = p.status as MessageSegment['status']; item.reason = typeof p.reason === 'string' ? p.reason : undefined; item.truncated = false
        item.references = item.status === 'confirmed' ? references(p.references) : []
      }
      put(item); continue
    }
    if (event.kind === 'message' && p.role === 'assistant' || event.kind === 'delivery') {
      if (stale) continue
      if (event.kind === 'message' && !String(p.content || '').trim()) continue
      const item = message(event)
      item.content = String(p.content || item.content); item.status = 'confirmed'; item.truncated = false
      item.references = references(p.references)
      if (event.kind === 'delivery') { item.source = 'delivery'; item.delivery = event }
      put(item); continue
    }
    if (event.kind === 'tool_activity' || event.kind === 'tool_step') {
      if (stale || event.kind === 'tool_activity' && p.version !== 1) continue
      const item = group(event), callId = String(p.call_id || `legacy-${event.seq}`), index = item.tools.findIndex(tool => tool.callId === callId)
      const old = index >= 0 ? item.tools[index] : undefined
      const status = String(p.status || 'pending'), phase = String(p.phase || (['pending', 'running'].includes(status) ? 'pending' : 'finished'))
      if (old && ['finished', 'restored'].includes(old.phase) && old.status === 'success' && ['started', 'restored', 'pending'].includes(phase)) continue
      const tool: ToolStep = { id: `${run}:${callId}`, callId, operationId: typeof p.operation_id === 'string' ? p.operation_id : old?.operationId, tool: String(p.tool || old?.tool || '工具'), phase, status, summary: String(p.summary || old?.summary || ''), created: old?.created || event.created, seq: event.seq, jobRefs: references(p.job_refs).length ? references(p.job_refs) : old?.jobRefs || [], events: [...(old?.events || []), event] }
      if (index < 0) item.tools.push(tool); else item.tools[index] = tool
      put(item); continue
    }
    if (event.kind === 'job_progress') {
      if (stale || !p.job_ref || typeof p.job_ref !== 'object') continue
      const item = group(event), ref = p.job_ref as AgentRef, id = jobKey(ref)
      const old = latestJob(result.items.flatMap(item => item.type === 'activity' ? item.jobs : []).filter(job => job.id === id))
      const value = mergeJob(old, jobProgress(p as JobFact, event.seq, undefined, event.run_id))
      const index = item.jobs.findIndex(job => job.id === id)
      if (index < 0) item.jobs.push(value); else item.jobs[index] = value
      put(item); continue
    }
    if (['model_request', 'usage', 'context_injection', 'model_transport', 'response_transport', 'stream_capability'].includes(event.kind)) { const item = group(event); item.diagnostics.push(event); put(item); continue }
    if (event.kind === 'context_compaction' || event.kind === 'delivery_check') {
      if (stale) continue
      const id = event.kind === 'delivery_check' ? `delivery-check:${run}` : p.compaction_id ? `compaction:${run}:${String(p.compaction_id)}` : `event:${event.seq}`
      put({ type: 'event', id, seq: event.seq, event }); continue
    }
    if (event.kind === 'cancelled' || event.kind === 'error' || event.kind === 'task_control' && p.manual) interrupt(event.run_id, String(p.message || (p.manual ? '已切换人工处理' : '本轮已停止')))
    if (event.kind === 'message') put({ type: 'message', id: `event:${event.seq}`, seq: event.seq, runId: event.run_id, generation, created: event.created, role: 'user', content: String(p.content || ''), source: 'user', status: 'confirmed', revision: 1, references: references(p.references), queued: p.queued === true })
    else if (['question', 'error', 'cancelled', 'task_control', 'skill_activated', 'skill_deactivated', 'context_compaction', 'delivery_check'].includes(event.kind)) put({ type: 'event', id: `event:${event.seq}`, seq: event.seq, event })
  }
  return result
}
export const projectConversation = (events: AgentEvent[]) => applyConversationEvents(createProjection(), events)
