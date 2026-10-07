import type { AgentRef } from './api'

export function referencePath(ref: AgentRef, taskId?: string) {
  const params = new URLSearchParams({ workspace: ref.workspace_id })
  if (taskId) params.set('task_return', taskId)
  if (ref.kind === 'evidence') {
    params.set('evidence', ref.object_id)
    if (taskId || ref.parent_id) params.set('session', taskId || ref.parent_id!)
    return `/findings?${params}`
  }
  if (ref.kind === 'artifact') { params.set('bundle', ref.object_id); return `/artifacts?${params}` }
  if (ref.kind === 'asset' || ref.kind === 'asset_set') {
    params.set(ref.kind === 'asset' ? 'asset' : 'set', ref.object_id)
    return `/assets?${params}`
  }
  if (ref.kind === 'job') {
    if (ref.coverage?.kind === 'screen_batch') {
      params.set('screen_batch', ref.object_id)
      if (ref.coverage.asset_set_id) params.set('set', String(ref.coverage.asset_set_id))
      if (ref.parent_id) params.set('handoff', ref.parent_id)
      return `/apps/ocr/workflow?${params}`
    }
    if (ref.coverage?.kind === 'batch') { params.set('batch', ref.object_id); return `/batches?${params}` }
    params.set('job', ref.object_id); return `/jobs?${params}`
  }
  if (ref.application === 'data') {
    if (ref.parent_id) params.set('session', ref.parent_id)
    params.set(ref.kind, ref.object_id)
    return `/apps/data?${params}`
  }
  if (ref.application === 'forensics') { params.set('inspection', ref.object_id); return `/apps/forensics?${params}` }
  if (ref.application === 'ocr' || ref.application === 'classification') {
    if (ref.coverage?.origin === 'native_binding' && ref.parent_id) params.set('native_binding', ref.parent_id)
    else if (ref.parent_id) params.set('handoff', ref.parent_id)
    params.set('native_result', ref.object_id); params.set('revision', String(ref.revision))
    return `/apps/${ref.application}/workflow?${params}`
  }
  if (taskId) params.set('session', taskId)
  return `/?${params}`
}
