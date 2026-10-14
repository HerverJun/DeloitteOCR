import { useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Input, Modal, Select, Typography } from 'antd'
import { Link } from 'react-router-dom'
import { HttpError, localReviewer, request } from '../batches/api'
import { auditCommand, auditProjectPath, auditRequestStatus, pendingAudit, pendingFailure, persistAudit, replayAudit,
  type AuditPayload, type AuditProject, type AuditProcedure, type AuditReceipt, type PendingAudit } from '../audit/api'
import type { Finding } from './FindingsWorkspace'

async function options<T>(path: string, signal: AbortSignal): Promise<T[]> {
  const items: T[] = [], seen = new Set<string>()
  let cursor: string | null = null, token: string | null = null
  do {
    const query = new URLSearchParams({ limit: '100' })
    if (cursor) query.set('cursor', cursor)
    if (token) query.set('snapshot_token', token)
    const page = await request<{ items: T[]; next_cursor: string | null; snapshot_token: string }>(`${path}?${query}`, { signal })
    items.push(...page.items); token = page.snapshot_token; cursor = page.next_cursor
    if (cursor && seen.has(cursor)) throw new Error('档案目录分页已变化，请重新打开核对。')
    if (cursor) seen.add(cursor)
  } while (cursor)
  return items
}

export function FindingTransfer({ workspace, finding, close, done }: { workspace: string; finding: Finding | null; close: () => void; done: () => void }) {
  const client = useQueryClient()
  const [pending, setPending] = useState(() => pendingAudit(workspace))
  const original = pending && 'source_draft' in pending.payload ? pending : null
  const [projectId, setProjectId] = useState(original?.project_id || '')
  const [procedureId, setProcedureId] = useState(original?.payload && 'procedure_id' in original.payload ? original.payload.procedure_id : '')
  const [actor, setActor] = useState(() => original?.payload.actor || localReviewer())
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [receipt, setReceipt] = useState<AuditReceipt | null>(null)
  const projects = useQuery({ queryKey: ['audit-transfer-projects', workspace],
    queryFn: ({ signal }) => options<AuditProject>(`/workspaces/${encodeURIComponent(workspace)}/audit-projects`, signal) })
  const procedures = useQuery({ queryKey: ['audit-transfer-procedures', workspace, projectId], enabled: !!projectId,
    queryFn: ({ signal }) => options<AuditProcedure>(`${auditProjectPath(projectId)}/procedures`, signal) })
  const project = projects.data?.find(item => item.project_id === projectId)
  const source = original && 'source_draft' in original.payload ? original.payload.source_draft : finding?.transfer_source
  const target = new URLSearchParams({ workspace })
  if (projectId) target.set('project', projectId)
  if (procedureId) target.set('procedure', procedureId)
  if (receipt && 'entity_id' in receipt) target.set('finding', receipt.entity_id)
  const remember = (value: PendingAudit | null) => { persistAudit(workspace, value); setPending(value) }
  async function submit() {
    if (busy) return
    setBusy(true); setError('')
    let command: PendingAudit | null = pendingAudit(workspace)
    try {
      if (command && !('source_draft' in command.payload)) throw new Error('正式档案另有待核实操作，请先在正式档案中处理。')
      if (!command) {
        if (!source || !project || project.workspace_id !== workspace || !procedures.data?.some(item => item.procedure_id === procedureId) || !actor.display_name.trim()) throw new Error('明确选择目标档案、程序并填写本机操作人。')
        const payload = { ...auditCommand(actor), expected_revision: project.revision, procedure_id: procedureId, source_draft: source } as AuditPayload
        command = { workspace_id: workspace, project_id: projectId, path: `${auditProjectPath(projectId)}/findings`, payload, label: '固定版本草稿转档', status: 'uncertain' }
        remember(command)
      }
      let committed: AuditReceipt | null = null
      if (pending) {
        try {
          const status = await auditRequestStatus(command)
          if (status.state === 'committed' && status.receipt) committed = status.receipt
          else throw new Error('原转档仍待核实，请保留同一请求，在正式档案中查看状态。')
        } catch (caught) { if (!(caught instanceof HttpError) || caught.status !== 404) throw caught }
      }
      committed ||= await replayAudit(command)
      remember(null); setReceipt(committed); done()
      await client.invalidateQueries({ queryKey: ['audit', workspace] })
    } catch (caught) {
      const failure = caught as Error
      if (command && 'source_draft' in command.payload) remember(pendingFailure(failure, command))
      setError(failure.message)
    } finally { setBusy(false) }
  }
  return <Modal title="将固定版本草稿转入正式档案" open onCancel={close} footer={receipt ? <Button onClick={close}>完成</Button> : <Button type="primary" loading={busy}
    disabled={!!pending && !original || !pending && (!source || !projectId || !procedureId || !actor.display_name.trim() || projects.isFetching || procedures.isFetching)} onClick={() => void submit()}>{original ? '核实原转档' : '确认转档'}</Button>}>
    <Typography.Paragraph>选择已有正式档案和审计程序。转档保留事实、解释、口径、待补信息及固定证据，生成正式草稿；编制和复核署名由人工另行登记。</Typography.Paragraph>
    {source && <Typography.Paragraph type="secondary">源草稿 {source.draft_id} · v{source.revision} · 固定资料范围 v{source.scope_revision}</Typography.Paragraph>}
    {(error || projects.error || procedures.error) && <Alert type="error" title={error || projects.error?.message || procedures.error?.message} />}
    {pending && <Alert type="warning" title={original ? '原转档请求已保留，请核实同一操作' : '正式档案有其它待核实操作'} description={pending.reason !== error ? pending.reason : undefined} />}
    {receipt ? <><Alert type="success" title="已转入正式档案，源草稿仍保留" /><Link to={`/audit?${target}`}>查看正式发现与固定证据</Link></> : <>
      <label className="audit-field">目标正式档案<Select aria-label="目标正式档案" value={projectId || undefined} disabled={!!pending || busy} loading={projects.isPending}
        options={projects.data?.map(item => ({ value: item.project_id, label: item.project.title }))} onChange={value => { setProjectId(value); setProcedureId('') }} /></label>
      <label className="audit-field">目标审计程序<Select aria-label="目标审计程序" value={procedureId || undefined} disabled={!!pending || busy || !projectId} loading={procedures.isFetching}
        options={procedures.data?.map(item => ({ value: item.procedure_id, label: item.procedure.title }))} onChange={setProcedureId} /></label>
      <label className="audit-field">本机转档操作人<Input aria-label="本机转档操作人" value={actor.display_name} maxLength={100} disabled={!!pending || busy} onChange={event => {
        const value = { ...actor, display_name: event.target.value }; setActor(value); localStorage.setItem('clear-local-reviewer', JSON.stringify(value))
      }} /></label>
      <Link to={`/audit?${target}`}>{pending ? '打开正式档案核对原操作' : '管理正式档案与程序'}</Link>
    </>}
  </Modal>
}
