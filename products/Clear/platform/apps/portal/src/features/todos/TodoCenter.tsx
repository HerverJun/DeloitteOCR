import { useEffect, useState } from 'react'
import type { AuditEvidenceSnapshot } from '../../../../../packages/contracts/types.generated'
import { ForensicEvidenceView } from '../forensics/ForensicWorkbench'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Empty, Select, Space, Table, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import type { AuditFinding, AuditProcedurePage, AuditProjectPage, ItemEvidence, TodoItem, TodoLocate, TodoPage } from '../../../../../packages/contracts/types.generated'
import { useWorkspaceId } from '../../App'
import { HttpError, request, scenarioNames, stateNames, type Batch } from '../batches/api'
import { ItemEvidenceView } from './ItemEvidenceView'
import './todos.css'

const todoScenarioNames = { ...scenarioNames, image_forensics: '图像取证' }
const { Title, Text, Paragraph } = Typography
const PAGE_SIZE = 20
const priorityNames = { high: '高', normal: '普通', low: '低' }
const typeNames = { result: '处理结果', technical: '技术故障', investigation: '调查事项' }
const stateLabel = (value: string) => ({ draft: '尚未解决', resolved: '已解决' } as Record<string, string>)[value] || stateNames[value] || value
const filters = ['kind', 'issue_type', 'scenario', 'disposition', 'finding_status', 'execution_state', 'project_id', 'procedure_id', 'priority', 'sort']
function queryFor(params: URLSearchParams, includePage = true) {
  const query = new URLSearchParams({ limit: String(PAGE_SIZE), sort: params.get('sort') === 'updated' ? 'updated' : 'priority_then_updated' })
  for (const name of filters) if (name !== 'sort' && params.get(name)) query.set(name, params.get(name)!)
  if (includePage) for (const name of ['cursor', 'snapshot_token']) if (params.get(name)) query.set(name, params.get(name)!)
  return query
}
async function allChoices<T extends AuditProjectPage | AuditProcedurePage>(path: string, signal: AbortSignal) {
  const items: T['items'] = []
  let cursor: string | null = null, token: string | null = null
  do {
    const query = new URLSearchParams({ limit: '200' })
    if (cursor && token) { query.set('cursor', cursor); query.set('snapshot_token', token) }
    const page: T = await request(`${path}?${query}`, { signal })
    if (token && token !== page.snapshot_token) throw new Error('筛选选项快照已变化，请刷新')
    items.push(...page.items as never[]); token = page.snapshot_token; cursor = page.next_cursor
  } while (cursor)
  return items
}
function Failure({ error, refresh }: { error: Error | null; refresh: () => void }) {
  return error && <Alert type={error instanceof HttpError && error.status === 409 ? 'warning' : 'error'} title={error.message}
    description={error instanceof HttpError && error.status === 409 ? '事项已更新，刷新完整列表快照后继续。原筛选和事项身份保留。' : undefined}
    action={<Button onClick={refresh}>刷新并保留筛选</Button>} />
}
function FindingEvidence({ workspaceId, finding, selected }: { workspaceId: string; finding: AuditFinding; selected: string }) {
  const [evidenceId, setEvidenceId] = useState(finding.evidence_refs[0]?.evidence_id || '')
  useEffect(() => setEvidenceId(finding.evidence_refs[0]?.evidence_id || ''), [selected])
  const evidence = useQuery({ queryKey: ['todo-evidence', workspaceId, finding.project_id, evidenceId], enabled: !!evidenceId, refetchOnMount: 'always',
    queryFn: ({ signal }) => request<AuditEvidenceSnapshot>(`/audit-projects/${encodeURIComponent(finding.project_id)}/evidence/${encodeURIComponent(evidenceId)}`, { signal }) })
  return <><Paragraph><Text strong>调查解释：</Text>{finding.finding.explanation || '尚未填写'}</Paragraph><Paragraph><Text strong>调查结论：</Text>{finding.finding.conclusion || '尚未填写'}</Paragraph>
    <Space wrap><Text>固定证据 {finding.evidence_refs.length} 项</Text><Select aria-label="调查证据选择" value={evidenceId || undefined} onChange={setEvidenceId}
      options={finding.evidence_refs.map((item, index) => ({ value: item.evidence_id, label: 'source_evidence_id' in item ? `证据 ${index + 1} · 直接工具 · 固定版本` : 'inspection_id' in item ? `证据 ${index + 1} · 图像取证 · 固定版本 ${item.inspection_revision}` : `证据 ${index + 1} · ${item.batch_id} / ${item.item_id} · 固定版本 ${item.batch_revision}` }))} style={{ minWidth: 300 }} /></Space>
    <Failure error={evidence.error} refresh={() => void evidence.refetch()} />{evidence.isPending && evidenceId && <Paragraph>读取固定证据…</Paragraph>}
    {evidence.data && ('reference' in evidence.data ? <Paragraph>{evidence.data.summary}<a href={evidence.data.location_url}>查看直接结果来源</a></Paragraph> : evidence.data.scenario === 'image_forensics' ? <ForensicEvidenceView inspection={evidence.data.inspection} /> : <ItemEvidenceView evidence={evidence.data} />)}</>
}
function TodoWorkspace({ workspaceId }: { workspaceId: string }) {
  const [params, setParams] = useSearchParams()
  const [generation, setGeneration] = useState(0)
  const [previousCursors, setPreviousCursors] = useState<(string | null)[]>([])
  const selected = params.get('todo') || ''
  const query = queryFor(params)
  const base = `/workspaces/${encodeURIComponent(workspaceId)}/todos`
  const list = useQuery({ queryKey: ['todos', workspaceId, 'list', query.toString(), generation], refetchOnMount: 'always',
    queryFn: ({ signal }) => request<TodoPage>(`${base}?${query}`, { signal }) })
  const located = useQuery({ queryKey: ['todos', workspaceId, 'locate', selected, query.toString(), generation], enabled: !!selected, refetchOnMount: 'always',
    queryFn: ({ signal }) => request<TodoLocate>(`${base}/${encodeURIComponent(selected)}?${query}`, { signal }) })
  const item = located.data?.item
  const scopeValid = !item || item.context.workspace_id === workspaceId
  const projectId = params.get('project_id') || ''
  const projects = useQuery({ queryKey: ['todos-projects', workspaceId], queryFn: ({ signal }) => allChoices<AuditProjectPage>(`/workspaces/${encodeURIComponent(workspaceId)}/audit-projects`, signal) })
  const procedures = useQuery({ queryKey: ['todos-procedures', workspaceId, projectId], enabled: !!projectId,
    queryFn: ({ signal }) => allChoices<AuditProcedurePage>(`/audit-projects/${encodeURIComponent(projectId)}/procedures`, signal) })
  const batch = useQuery({ queryKey: ['todo-batch', workspaceId, item?.context.batch_id, generation], enabled: scopeValid && !!item?.context.batch_id, refetchOnMount: 'always',
    queryFn: ({ signal }) => request<Batch>(`/batches/${encodeURIComponent(item!.context.batch_id!)}`, { signal }) })
  const evidence = useQuery({ queryKey: ['todo-evidence', workspaceId, item?.context.batch_id, item?.context.item_id, item?.batch_revision, generation], refetchOnMount: 'always',
    enabled: scopeValid && item?.kind === 'issue' && !!item.context.batch_id && !!item.context.item_id && !!item.batch_revision,
    queryFn: ({ signal }) => request<ItemEvidence>(`/batches/${encodeURIComponent(item!.context.batch_id!)}/items/${encodeURIComponent(item!.context.item_id!)}/evidence?batch_revision=${item!.batch_revision}`, { signal }) })
  const finding = useQuery({ queryKey: ['todo-finding', workspaceId, item?.context.project_id, item?.context.finding_id, item?.project_revision, generation], refetchOnMount: 'always',
    enabled: scopeValid && item?.kind === 'finding' && !!item.context.project_id && !!item.context.finding_id,
    queryFn: ({ signal }) => request<AuditFinding>(`/audit-projects/${encodeURIComponent(item!.context.project_id!)}/findings/${encodeURIComponent(item!.context.finding_id!)}`, { signal }) })
  const nativeScenario = item?.scenario === 'ocr' || item?.scenario === 'classification'
  const nativeProjects = useQuery({ queryKey: ['todo-native-projects', workspaceId, item?.scenario, generation], enabled: scopeValid && nativeScenario, refetchOnMount: 'always',
    queryFn: ({ signal }) => request<{ items: { project_id: string; link_id: string }[] }>(`/workspaces/${encodeURIComponent(workspaceId)}/${item!.scenario}/projects`, { signal }) })
  const index = list.data?.items.findIndex(row => row.todo_id === selected) ?? -1
  const selectedPage = useQuery({ queryKey: ['todos', workspaceId, 'selected-page', selected, located.data?.page_cursor, located.data?.snapshot_token, queryFor(params, false).toString(), generation],
    enabled: !!selected && index < 0 && !!located.data?.matches_filters, refetchOnMount: 'always',
    queryFn: ({ signal }) => { const next = queryFor(params, false); if (located.data!.page_cursor) next.set('cursor', located.data!.page_cursor!); next.set('snapshot_token', located.data!.snapshot_token); return request<TodoPage>(`${base}?${next}`, { signal }) } })
  const continuationPage = index >= 0 ? list.data : selectedPage.data
  const continuationIndex = continuationPage?.items.findIndex(row => row.todo_id === selected) ?? -1
  const beyond = useQuery({ queryKey: ['todos', workspaceId, 'next-page-preview', continuationPage?.next_cursor, continuationPage?.snapshot_token, queryFor(params, false).toString(), generation],
    enabled: !!selected && continuationIndex >= 0 && continuationIndex === (continuationPage?.items.length || 0) - 1 && !!continuationPage?.next_cursor,
    queryFn: ({ signal }) => { const next = queryFor(params, false); next.set('cursor', continuationPage!.next_cursor!); next.set('snapshot_token', continuationPage!.snapshot_token); return request<TodoPage>(`${base}?${next}`, { signal }) } })
  const nextId = continuationIndex >= 0 ? continuationPage?.items[continuationIndex + 1]?.todo_id || beyond.data?.items[0]?.todo_id : undefined
  const returnTo = `/todos?${params}`
  function change(name: string, value?: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value); else next.delete(name)
    for (const field of ['cursor', 'snapshot_token']) next.delete(field)
    if (name === 'project_id') next.delete('procedure_id')
    if (name === 'disposition' && value) { next.set('kind', 'issue'); next.delete('finding_status') }
    if (name === 'finding_status' && value) { next.set('kind', 'finding'); next.delete('disposition') }
    if (name === 'kind') { if (value === 'finding') next.delete('disposition'); else if (value === 'issue') next.delete('finding_status') }
    setPreviousCursors([]); setParams(next)
  }
  function refresh() {
    const next = new URLSearchParams(params); next.delete('cursor'); next.delete('snapshot_token'); setParams(next); setPreviousCursors([]); setGeneration(value => value + 1)
  }
  function open(todoId: string) { const next = new URLSearchParams(params); next.set('todo', todoId); setParams(next) }
  function jumpToLocatedPage() {
    if (!located.data?.matches_filters) return
    const next = new URLSearchParams(params)
    if (located.data.page_cursor) next.set('cursor', located.data.page_cursor); else next.delete('cursor')
    next.set('snapshot_token', located.data.snapshot_token); setPreviousCursors([]); setParams(next)
  }
  // Opening a linked identity reads it directly; moving to its list page is explicit and keeps every filter.
  function batchUrl(row: TodoItem) {
    const next = new URLSearchParams({ workspace: workspaceId, batch: row.context.batch_id!, item: row.context.item_id!, todo: row.todo_id, return_to: returnTo })
    next.set('disposition', 'all')
    if (nextId) next.set('next_todo', nextId)
    return `/batches?${next}`
  }
  function auditUrl(row: TodoItem) {
    const next = new URLSearchParams({ workspace: workspaceId, project: row.context.project_id!, return_to: returnTo })
    if (row.context.procedure_id) next.set('procedure', row.context.procedure_id)
    if (row.context.finding_id) next.set('finding', row.context.finding_id)
    return `/audit?${next}`
  }
  const nativeParams = new URLSearchParams({ workspace: workspaceId, return_to: returnTo })
  const nativeProject = nativeProjects.data?.items.find(project => project.link_id === batch.data?.template.config.link_id)
  if (nativeProject) nativeParams.set('project', nativeProject.project_id)
  const imports = batch.data?.receipts.native_import as { handoff_id?: string } | undefined
  const inputs = batch.data?.receipts.native_inputs as { asset_set_id?: string } | undefined
  if (imports?.handoff_id) nativeParams.set('handoff', imports.handoff_id)
  if (inputs?.asset_set_id) nativeParams.set('set', inputs.asset_set_id)
  return <><section className="panel todo-filters"><div className="section-head"><Title level={3}>筛选全量事项</Title><Button onClick={refresh}>刷新全部并保留筛选</Button></div>
    <div className="todo-filter-grid"><Select aria-label="待办类别" value={params.get('kind') || undefined} allowClear placeholder="全部处理与调查" onChange={value => change('kind', value)} options={[{ value: 'issue', label: '批次事项' }, { value: 'finding', label: '调查事项' }]} />
      <Select aria-label="待办事项类型" value={params.get('issue_type') || undefined} allowClear placeholder="全部事项类型" onChange={value => change('issue_type', value)} options={Object.entries(typeNames).map(([value, label]) => ({ value, label }))} />
      <Select aria-label="待办处理场景" value={params.get('scenario') || undefined} allowClear placeholder="全部处理场景" onChange={value => change('scenario', value)} options={Object.entries(todoScenarioNames).map(([value, label]) => ({ value, label }))} />
      <Select aria-label="待办批次处置" value={params.get('disposition') || undefined} allowClear placeholder="全部批次处置" onChange={value => change('disposition', value)} options={['pending', 'confirmed', 'deferred'].map(value => ({ value, label: stateNames[value] }))} />
      <Select aria-label="待办调查状态" value={params.get('finding_status') || undefined} allowClear placeholder="全部调查状态" onChange={value => change('finding_status', value)} options={[{ value: 'draft', label: '尚未解决' }, { value: 'resolved', label: '已解决' }]} />
      <Select aria-label="待办执行状态" value={params.get('execution_state') || undefined} allowClear placeholder="全部执行状态" onChange={value => change('execution_state', value)} options={['queued', 'running', 'partial', 'succeeded', 'failed', 'cancelled', 'needs_verification', 'waiting_input', 'waiting_resource'].map(value => ({ value, label: stateNames[value] }))} />
      <Select aria-label="待办审计项目" value={projectId || undefined} allowClear placeholder="全部审计项目" onChange={value => change('project_id', value)} loading={projects.isPending} options={projects.data?.map(project => ({ value: project.project_id, label: project.project.title }))} />
      <Select aria-label="待办审计程序" value={params.get('procedure_id') || undefined} allowClear placeholder="全部审计程序" disabled={!projectId} loading={!!projectId && procedures.isPending} onChange={value => change('procedure_id', value)} options={procedures.data?.map(procedure => ({ value: procedure.procedure_id, label: procedure.procedure.title }))} />
      <Select aria-label="待办优先级" value={params.get('priority') || undefined} allowClear placeholder="全部优先级" onChange={value => change('priority', value)} options={Object.entries(priorityNames).map(([value, label]) => ({ value, label }))} />
      <Select aria-label="待办排序" value={params.get('sort') || 'priority_then_updated'} onChange={value => change('sort', value)} options={[{ value: 'priority_then_updated', label: '优先级后按更新时间' }, { value: 'updated', label: '按更新时间' }]} /></div>
    <Paragraph type="secondary">筛选和排序由服务端对全量事项执行。技术故障独立展示；调查优先级来自人工设置，批次事项按默认复核顺序。</Paragraph>
    <Failure error={projects.error} refresh={() => void projects.refetch()} /><Failure error={procedures.error} refresh={() => void procedures.refetch()} /></section>
    <div className="todo-layout"><section className="panel"><div className="section-head"><Title level={3}>事项列表</Title><Text>共 {list.data?.total ?? '…'} 项</Text></div>
      <Failure error={list.error} refresh={refresh} />
      <Table<TodoItem> size="small" rowKey="todo_id" dataSource={list.data?.items || []} pagination={false} loading={list.isPending} rowClassName={row => row.todo_id === selected ? 'todo-selected' : ''}
        columns={[{ title: '事项', key: 'title', render: (_, row) => <Button type="link" className="batch-issue-link" onClick={() => open(row.todo_id)}>{row.title}</Button> },
          { title: '类型', dataIndex: 'issue_type', render: value => typeNames[value as keyof typeof typeNames] }, { title: '状态', dataIndex: 'state', render: stateLabel },
          { title: '优先级', dataIndex: 'priority', render: value => priorityNames[value as keyof typeof priorityNames] }]} />
      <Space className="application-message" wrap><Button disabled={!previousCursors.length} onClick={() => { const cursor = previousCursors.at(-1); const next = new URLSearchParams(params); if (cursor) next.set('cursor', cursor); else next.delete('cursor'); setParams(next); setPreviousCursors(values => values.slice(0, -1)) }}>上一页</Button>
        <Button disabled={!list.data?.next_cursor} onClick={() => { const next = new URLSearchParams(params); setPreviousCursors(values => [...values, params.get('cursor')]); next.set('cursor', list.data!.next_cursor!); next.set('snapshot_token', list.data!.snapshot_token); setParams(next) }}>下一页</Button>
        {!!params.get('cursor') && <Button onClick={refresh}>回到第一页</Button>}</Space></section>
      <section className="panel todo-detail">{!selected ? <Empty description="选择事项，阅读字段对照与证据，再进入批次或调查继续处理" /> : <>
        <Failure error={located.error} refresh={refresh} />{located.isPending && <Paragraph>按事项身份读取当前证据…</Paragraph>}
        {!scopeValid && <Alert type="error" title="此事项属于其他工作空间，停止读取上下文" />}
        {item && scopeValid && <><div className="section-head"><Title level={3}>{item.title}</Title><Tag color={item.issue_type === 'technical' ? 'error' : 'default'}>{typeNames[item.issue_type]}</Tag></div>
          <Paragraph>{stateLabel(item.state)} · 优先级 {priorityNames[item.priority]}（{item.priority_basis === 'manual_investigation' ? '人工调查设置' : '默认复核顺序'}） · {item.scenario ? todoScenarioNames[item.scenario] : '跨场景调查'}</Paragraph>
          <Paragraph type="secondary">{item.source_summary}</Paragraph>
          {!located.data!.matches_filters && <Alert type="info" title="此事项已不符合当前筛选" description="仍按原事项身份显示，原筛选保留。可刷新或继续处理该事项。" />}
          <Failure error={selectedPage.error || beyond.error} refresh={refresh} />
          <Space wrap className="todo-actions">{located.data!.matches_filters && index < 0 && <Button onClick={jumpToLocatedPage}>定位到当前列表页</Button>}
            {nextId && <Button onClick={() => open(nextId)}>下一待办事项</Button>}
            {item.context.batch_id && item.context.item_id && <Link to={batchUrl(item)}>打开并处理此批次事项</Link>}
            {item.context.project_id && <Link to={auditUrl(item)}>打开审计调查上下文</Link>}
            {nativeScenario && nativeProject && item.actions.includes('open_native') && <Link to={`/apps/${item.scenario}/workflow?${nativeParams}`}>到原生项目核对</Link>}</Space>
          <Failure error={evidence.error} refresh={refresh} />{item.kind === 'issue' && evidence.isPending && <Paragraph>读取当前固定证据…</Paragraph>}
          {evidence.data && <ItemEvidenceView evidence={evidence.data} />}
          <Failure error={finding.error} refresh={refresh} />{finding.data && <FindingEvidence key={finding.data.finding_id} workspaceId={workspaceId} finding={finding.data} selected={selected} />}
          <Failure error={batch.error} refresh={() => void batch.refetch()} /><Failure error={nativeProjects.error} refresh={() => void nativeProjects.refetch()} />
        </>}
      </>}</section></div>
  </>
}
export function TodoCenter() {
  const workspaceId = useWorkspaceId()
  return <div className="todo-center"><div className="page-heading"><Text className="eyebrow">AUDIT / TODOS</Text><Title level={1}>统一待办</Title>
    <Paragraph>从处理结果、技术故障和调查事项进入实际证据，保留筛选、原生核对和业务处理上下文。</Paragraph></div>
    {workspaceId ? <TodoWorkspace key={workspaceId} workspaceId={workspaceId} /> : <Empty description="先在顶部选择工作空间" />}</div>
}
