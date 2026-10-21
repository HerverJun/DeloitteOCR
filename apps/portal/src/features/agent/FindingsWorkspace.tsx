import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { agentRequest, agentWrite, requestId, type AuditTask, type AgentRef } from './api'
import { useWorkspaceId } from '../../App'
import { EvidenceContent } from './EvidenceContent'
import { FindingTransfer } from './FindingTransfer'
import { pendingAudit } from '../audit/api'

export type Finding = { draft_id: string; task_id: string; revision: number; title: string; facts: string; interpretation: string; missing_information: string; recommendations: string; rule: string; evidence_ids: string[]; status: string; outdated: boolean; outdated_reasons: string[]; author: { kind: string; model?: string };
  transfer_source?: { task_id: string; draft_id: string; revision: number; snapshot_hash: string; scope_revision: number; project_context_revision: number };
  formal_links?: { project_id: string; procedure_id: string; finding_id: string; source_revision: number; source_changed: boolean }[] }
type Evidence = { evidence_id: string; summary: string; reference: AgentRef; sources: { asset_id: string; asset_revision: number; page_number?: number; row_number?: number; sheet_name?: string }[]; observation: unknown; location_url: string; stale?: boolean }
type FixedFinding = Pick<Finding, 'draft_id' | 'task_id' | 'revision' | 'title' | 'facts' | 'interpretation' | 'missing_information' | 'recommendations' | 'rule' | 'evidence_ids' | 'status' | 'author'> & {
  content_sha256: string; source_assets: string[]; workspace_id?: string
}
const blank: Finding = { draft_id: '', task_id: '', revision: 0, title: '', facts: '', interpretation: '', missing_information: '', recommendations: '', rule: '', evidence_ids: [], status: 'draft', outdated: false, outdated_reasons: [], author: { kind: 'human' } }

export function FindingsWorkspace() {
  const workspace = useWorkspaceId(), [params, setParams] = useSearchParams(), client = useQueryClient()
  const taskId = params.get('session')
  const [editing, setEditing] = useState<Finding | null>(null), [evidence, setEvidence] = useState<Evidence | null>(null), [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const [transfer, setTransfer] = useState<Finding | null>(null), [recoverTransfer, setRecoverTransfer] = useState(false)
  const tasks = useQuery({ queryKey: ['audit-tasks', workspace], enabled: !!workspace, queryFn: () => agentRequest<{ items: AuditTask[] }>(`/workspaces/${workspace}/audit-tasks`) })
  const task = useQuery({ queryKey: ['audit-task', taskId], enabled: !!taskId, queryFn: () => agentRequest<AuditTask>(`/agent/sessions/${taskId}/task`) })
  const list = useQuery({ queryKey: ['audit-findings', taskId], enabled: !!taskId, queryFn: () => agentRequest<{ items: Finding[]; total: number }>(`/agent/sessions/${taskId}/findings`) })
  const draftId = params.get('draft'), revision = params.get('revision'), hash = params.get('sha256')
  const fixedOpen = !!draftId || params.has('revision') || params.has('sha256')
  const fixedValid = !!(taskId && draftId && revision && /^[1-9]\d*$/.test(revision) && Number.isSafeInteger(Number(revision)) && hash && /^[a-f0-9]{64}$/.test(hash))
  const fixed = useQuery({ queryKey: ['audit-draft-revision', workspace, taskId, draftId, revision, hash], enabled: !!workspace && fixedValid,
    queryFn: async () => {
      const value = await agentRequest<FixedFinding>(`/agent/sessions/${encodeURIComponent(taskId!)}/findings/${encodeURIComponent(draftId!)}/revisions/${revision}?sha256=${hash}`)
      if (value.draft_id !== draftId || value.task_id !== taskId || value.revision !== Number(revision) || value.content_sha256 !== hash || value.workspace_id && value.workspace_id !== workspace) throw new Error('读取的草稿与固定版本链接不一致，请重新核实交付引用。')
      return value
    } })
  const closeFixed = () => { const next = new URLSearchParams(params); for (const key of ['draft', 'revision', 'sha256']) next.delete(key); setParams(next) }
  const evidenceId = params.get('evidence')
  useEffect(() => {
    let current = true
    if (taskId && evidenceId) {
      void agentRequest<Evidence>(`/agent/sessions/${taskId}/evidence/${evidenceId}`)
        .then(value => { if (current) setEvidence(value) })
        .catch(e => { if (current) setError((e as Error).message) })
    }
    return () => { current = false }
  }, [taskId, evidenceId])
  const refresh = () => { void list.refetch(); void client.invalidateQueries({ queryKey: ['audit-overview', workspace] }) }
  const save = async () => {
    if (!editing || !taskId) return
    const { draft_id, revision, title, facts, interpretation, missing_information, recommendations, rule, evidence_ids, status } = editing
    const command = { expected_revision: revision, draft_id: draft_id || null, title, facts, interpretation, missing_information, recommendations, rule, evidence_ids, status }
    const key = `clear-finding-edit:${taskId}:${draft_id || 'new'}`, stamp = JSON.stringify(command)
    const old = JSON.parse(sessionStorage.getItem(key) || 'null') as { stamp: string; request_id: string } | null
    const attempt = old?.stamp === stamp ? old : { stamp, request_id: requestId() }; sessionStorage.setItem(key, JSON.stringify(attempt))
    setBusy(true); setError('')
    try { await agentWrite(`/agent/sessions/${taskId}/findings`, { ...command, request_id: attempt.request_id }, 'PUT'); sessionStorage.removeItem(key); setEditing(null); refresh() }
    catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const showEvidence = async (id: string, sourceTask: string) => {
    try { setEvidence(await agentRequest<Evidence>(`/agent/sessions/${sourceTask}/evidence/${id}`)) } catch (e) { setError((e as Error).message) }
  }
  const bind = async (ref: AgentRef) => {
    if (!editing || !taskId) return
    try { const bound = await agentWrite<Evidence>(`/agent/sessions/${taskId}/evidence`, { reference: ref }); setEditing({ ...editing, evidence_ids: Array.from(new Set([...editing.evidence_ids, bound.evidence_id])) }) }
    catch (e) { setError((e as Error).message) }
  }
  const exportAll = async () => {
    if (!list.data?.items.length || !taskId) return
    const key = `clear-workpaper-export:${taskId}`, command = { draft_ids: list.data.items.map(item => item.draft_id), title: `${task.data?.title || '审计任务'} · 底稿草稿` }
    const stamp = JSON.stringify(list.data.items.map(item => [item.draft_id, item.revision]))
    const old = JSON.parse(sessionStorage.getItem(key) || 'null') as { stamp: string; request_id: string } | null
    const attempt = old?.stamp === stamp ? old : { stamp, request_id: requestId() }; sessionStorage.setItem(key, JSON.stringify(attempt))
    setBusy(true); setError('')
    try { const bundle = await agentWrite<{ bundle_id: string }>(`/agent/sessions/${taskId}/workpaper`, { ...command, request_id: attempt.request_id }); sessionStorage.removeItem(key)
      window.location.assign(`/artifacts?workspace=${workspace}&bundle=${bundle.bundle_id}`)
    } catch (e) { setError((e as Error).message) } finally { setBusy(false) }
  }
  const change = (key: keyof Finding, value: string) => setEditing(old => old ? { ...old, [key]: value } : old)
  return <div className="audit-findings"><Typography.Title level={1}>发现与底稿</Typography.Title><Typography.Paragraph type="secondary">从实际工具结果组织观察事实，编辑解释与待补信息，并生成可下载的底稿草稿。</Typography.Paragraph>
    {!workspace ? <Alert type="info" title="选择项目后查看审计发现" /> : <>
      <Space wrap><Select aria-label="发现所属任务" style={{ minWidth: 260 }} value={taskId || undefined} placeholder="选择审计任务" options={tasks.data?.items.map(item => ({ value: item.id, label: item.title }))}
        onChange={id => { const next = new URLSearchParams(params); next.set('session', id); for (const key of ['draft', 'revision', 'sha256']) next.delete(key); setParams(next); setError('') }} />
        <Button disabled={!taskId} onClick={() => setEditing({ ...blank, task_id: taskId || '' })}>新建发现草稿</Button>
        <Button disabled={!list.data?.items.length || busy} onClick={() => void exportAll()}>生成底稿草稿</Button>
        <Link to={`/agent?workspace=${workspace}${taskId ? `&session=${taskId}` : ''}`}>回到审计对话</Link></Space>
      {(error || list.error || tasks.error) && <Alert type="error" title={error || list.error?.message || tasks.error?.message} />}
      {(() => { const pending = pendingAudit(workspace); return pending && 'source_draft' in pending.payload ? <Alert type="warning" title="有草稿转档回执待核实" action={<Button onClick={() => setRecoverTransfer(true)}>核实原转档</Button>} /> : null })()}
      {list.data?.items.length ? <Table<Finding> rowKey="draft_id" dataSource={list.data.items} pagination={{ pageSize: 10 }} columns={[
        { title: '发现', dataIndex: 'title', render: (value, item) => <Button type="link" onClick={() => setEditing(item)}>{value}</Button> },
        { title: '状态', render: (_, item) => <Space><Tag>{({ draft: '草稿', confirmed: '已确认', dismissed: '已排除' } as Record<string, string>)[item.status]}</Tag>{item.outdated && <Tag color="warning">来源有更新</Tag>}</Space> },
        { title: '作者与版本', render: (_, item) => `${item.author.kind === 'agent' ? 'Agent · ' + (item.author.model || '') : '本机人工编辑'} / v${item.revision}` },
        { title: '证据', render: (_, item) => <Space wrap>{item.evidence_ids.map(id => <Button key={id} size="small" onClick={() => void showEvidence(id, item.task_id)}>查看来源</Button>)}</Space> },
        { title: '正式档案', render: (_, item) => <Space wrap>{item.formal_links?.map(link => <Link key={`${link.project_id}:${link.finding_id}`} to={`/audit?${new URLSearchParams({ workspace, project: link.project_id, procedure: link.procedure_id, finding: link.finding_id })}`}>正式记录 · 源 v{link.source_revision}{link.source_changed ? '（源已更新）' : ''}</Link>)}<Button size="small" disabled={!item.transfer_source || busy} onClick={() => setTransfer(item)}>转入正式档案</Button></Space> },
      ]} /> : <Empty description={taskId ? '任务执行后，Agent 可以把结果整理为发现；也可以先记录待补信息。' : '选择任务查看发现与草稿'} />}
    </>}
    {workspace && (transfer || recoverTransfer) && <FindingTransfer key={`${workspace}:${transfer?.draft_id || 'recovery'}`} workspace={workspace} finding={transfer} close={() => { setTransfer(null); setRecoverTransfer(false) }} done={refresh} />}
    <Modal title={editing?.draft_id ? '编辑发现草稿' : '新建发现草稿'} open={!!editing} onCancel={() => setEditing(null)} width={760} okText="保存草稿" cancelText="关闭" okButtonProps={{ disabled: !editing?.title.trim(), loading: busy }} onOk={() => void save()}>
      {editing && <>{error && <Alert type="error" title={error} />}{editing.outdated && <Alert type="warning" title="草稿引用的资料有更新" description={editing.outdated_reasons.join('；')} />}
        <label className="audit-field">发现标题<Input value={editing.title} maxLength={200} onChange={e => change('title', e.target.value)} /></label>
        {([['facts', '观察事实'], ['interpretation', '解释与判断'], ['rule', '采用口径'], ['missing_information', '待补信息'], ['recommendations', '处理建议']] as const).map(([key, label]) => <label key={key} className="audit-field">{label}<Input.TextArea value={editing[key]} rows={3} maxLength={key === 'facts' || key === 'interpretation' ? 12000 : 8000} onChange={e => change(key, e.target.value)} /></label>)}
        <Space wrap>{editing.evidence_ids.map(id => <Tag key={id} closable onClose={() => setEditing({ ...editing, evidence_ids: editing.evidence_ids.filter(value => value !== id) })}><button onClick={() => void showEvidence(id, editing.task_id)}>证据 {id.slice(0, 10)}</button></Tag>)}</Space>
        <Select<number> aria-label="添加实际结果证据" value={undefined} placeholder="选择任务结果，固定为证据" style={{ width: '100%', marginTop: 12 }} options={[...(task.data?.result_refs || []), ...(task.data?.artifact_refs || []), ...(task.data?.initial_refs || [])].map((ref, index) => ({ value: index, label: `${ref.application || '资料'} · ${ref.kind} · ${String(ref.revision).slice(0, 16)}` }))}
          onChange={index => { const refs = [...(task.data?.result_refs || []), ...(task.data?.artifact_refs || []), ...(task.data?.initial_refs || [])]; void bind(refs[index]) }} />
        <label className="audit-field">处理状态<Select value={editing.status} options={[{ value: 'draft', label: '草稿' }, { value: 'confirmed', label: '已确认' }, { value: 'dismissed', label: '已排除' }]} onChange={value => change('status', value)} /></label>
      </>}
    </Modal>
    <Modal title={`草稿历史版本${fixedValid ? ` · v${revision}` : ''}`} open={fixedOpen} onCancel={closeFixed} width={820}
      footer={<Button onClick={closeFixed}>返回当前草稿列表</Button>}>
      <Typography.Paragraph type="secondary">此处显示交付时保存的固定版本，只读查看。当前草稿可从任务列表另行编辑。</Typography.Paragraph>
      {!fixedValid ? <Alert type="warning" title="草稿版本链接不完整或无效，请从交付成果重新打开。" /> : fixed.error ? <Alert type="error" title={fixed.error.message} /> : fixed.isPending ? <Typography.Paragraph role="status">正在核实固定版本与来源…</Typography.Paragraph> : fixed.data && <div aria-label="固定草稿只读内容">
        <Typography.Title level={3}>{fixed.data.title}</Typography.Title>
        <Space wrap><Tag>v{fixed.data.revision} · 只读</Tag><Tag>{({ draft: '草稿', confirmed: '已确认', dismissed: '已排除' } as Record<string, string>)[fixed.data.status] || fixed.data.status}</Tag></Space>
        {([['facts', '观察事实'], ['interpretation', '解释与判断'], ['rule', '采用口径'], ['missing_information', '待补信息'], ['recommendations', '处理建议']] as const).map(([key, label]) => <section className="audit-fixed-draft-section" key={key}><Typography.Text strong>{label}</Typography.Text><Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{fixed.data[key] || '未填写'}</Typography.Paragraph></section>)}
        {!!fixed.data.evidence_ids.length && <Space wrap>{fixed.data.evidence_ids.map(id => <Button key={id} onClick={() => void showEvidence(id, fixed.data!.task_id)}>查看固定证据 {id.slice(0, 10)}</Button>)}</Space>}
        <details className="audit-fixed-draft-section"><summary>版本与来源</summary><Typography.Paragraph>内容 SHA256：<Typography.Text code>{fixed.data.content_sha256}</Typography.Text></Typography.Paragraph>
          <Typography.Paragraph type="secondary">{fixed.data.source_assets.length} 份来源资料；固定证据保留保存时的来源版本。</Typography.Paragraph>
        </details>
      </div>}
    </Modal>
    <Modal title="固定证据与来源" open={!!evidence} onCancel={() => setEvidence(null)} footer={<Button onClick={() => setEvidence(null)}>关闭</Button>} width={820}>
      {evidence && <>{evidence.stale && <Alert type="warning" title="结果已有更新，以下为保存时的固定快照" />}<Typography.Paragraph>{evidence.summary}</Typography.Paragraph><Link to={evidence.location_url}>打开工具中的结果版本 →</Link>
        <div className="audit-evidence-sources">{evidence.sources.map((source, i) => <a key={i} href={`/api/platform/v1/workspaces/${workspace}/assets/${source.asset_id}/content?revision=${source.asset_revision}${source.page_number ? '#page=' + source.page_number : ''}`} target="_blank" rel="noreferrer">原资料 v{source.asset_revision}{source.sheet_name ? ' · ' + source.sheet_name : ''}{source.page_number ? ' · 第' + source.page_number + '页' : ''}{source.row_number ? ' · 第' + source.row_number + '行' : ''}</a>)}</div>
        <EvidenceContent observation={evidence.observation} /><details><summary>查看完整保存内容</summary><pre className="audit-evidence-json">{JSON.stringify(evidence.observation, null, 2)}</pre></details>
      </>}
    </Modal>
  </div>
}
