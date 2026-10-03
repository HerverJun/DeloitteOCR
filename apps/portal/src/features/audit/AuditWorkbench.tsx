import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Empty, Form, Input, Pagination, Select, Space, Table, Tabs, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { useWorkspaceId } from '../../App'
import { nativeReviewReturn } from '../catalog/nativeReviewReturn'
import { API, HttpError, localReviewer, request, scenarioNames, stateNames, type Batch, type Issue, type IssuePage, type LocalActor } from '../batches/api'
import { auditArtifacts, auditCommand, auditProjectPath, auditRequestStatus, pendingAudit, pendingFailure, persistAudit, replayAudit,
  type AuditEvidenceRef, type AuditEvidenceSelection, type AuditFinding, type AuditFindingFields, type AuditFindingPage,
  type AuditPayload, type AuditProcedure, type AuditProcedureFields, type AuditProcedurePage,
  type AuditProject, type AuditProjectFields, type AuditProjectPage, type AuditReceipt, type AuditRecord,
  type PendingAudit, type ReviewBundle, type WorkpaperBundle } from './api'
import './audit.css'
import type { ForensicEvidenceSelection, ForensicEvidenceRef } from '../../../../../packages/contracts/types.generated'
import { ForensicPicker } from '../forensics/ForensicWorkbench'

const { Title, Text, Paragraph } = Typography
const PAGE_SIZE = 20
type CursorPage = { total: number; snapshot_token: string; next_cursor: string | null }
type Writer = { blocked: boolean; actor: LocalActor; pending: PendingAudit | null; submit: (path: string, payload: AuditPayload, label: string) => void }
const projectFields: AuditProjectFields = { title: '', audit_object: '', period_start: '', period_end: '', objective: '' }
const procedureFields: AuditProcedureFields = { title: '', basis: '', scope: '', note: '', conclusion: '' }
const findingFields: AuditFindingFields = { title: '', priority: 'normal', explanation: '', conclusion: '', status: 'draft' }
const priorityNames = { high: '高', normal: '普通', low: '低' }

function useAuditPage<T extends CursorPage>(workspaceId: string, path: string, revision = 0) {
  const [state, setState] = useState<{ cursors: (string | null)[]; index: number; token: string | null }>({ cursors: [null], index: 0, token: null })
  const reset = () => setState({ cursors: [null], index: 0, token: null })
  useEffect(reset, [path, revision])
  const query = new URLSearchParams({ limit: String(PAGE_SIZE) })
  const cursor = state.cursors[state.index]
  if (cursor) query.set('cursor', cursor)
  if (state.token) query.set('snapshot_token', state.token)
  const separator = path.includes('?') ? '&' : '?'
  const result = useQuery({ queryKey: ['audit', workspaceId, path, revision, query.toString()],
    queryFn: ({ signal }) => request<T>(`${path}${separator}${query}`, { signal }) })
  return { ...result, index: state.index, reset, previous: () => setState(value => ({ ...value, index: value.index - 1 })),
    next: () => { if (result.data?.next_cursor) setState(value => ({
      cursors: [...value.cursors.slice(0, value.index + 1), result.data!.next_cursor], index: value.index + 1, token: result.data!.snapshot_token,
    })) } }
}
function PageNavigation({ page }: { page: ReturnType<typeof useAuditPage<CursorPage>> }) {
  return <div className="audit-pagination"><Button disabled={page.index === 0} onClick={page.previous}>上一页</Button>
    <Text type="secondary">第 {page.index + 1} 页 · 共 {page.data?.total ?? '…'} 项</Text>
    <Button disabled={!page.data?.next_cursor} onClick={page.next}>下一页</Button>
    <Button onClick={() => { page.reset(); void page.refetch() }}>刷新列表</Button></div>
}
function ReadError({ error, refresh }: { error: Error | null; refresh: () => void }) {
  return error ? <Alert className="application-message" type="error" title={error.message}
    description={error instanceof HttpError && error.status === 409 ? '列表或所见版本发生变化，请刷新后继续。' : undefined}
    action={<Button onClick={refresh}>刷新</Button>} /> : null
}
function RecordControls({ kind, id, contentRevision, contextRevision, records, current, project, writer }: {
  kind: 'project' | 'procedure' | 'finding'; id: string; contentRevision: number; contextRevision: number;
  records: AuditRecord[]; current: boolean; project: AuditProject; writer: Writer
}) {
  const [note, setNote] = useState('')
  const prepared = records.some(item => item.role === 'prepared' && item.content_revision === contentRevision && item.project_context_revision === contextRevision)
  const submit = (role: 'prepared' | 'reviewed') => writer.submit(`${auditProjectPath(project.project_id)}/records`, {
    ...auditCommand(writer.actor), expected_revision: project.revision, entity_kind: kind, entity_id: id,
    content_revision: contentRevision, project_context_revision: contextRevision, role, note,
  }, role === 'prepared' ? '登记编制记录' : '登记复核记录')
  return <div className="audit-records"><Space wrap><Text strong>编制与复核</Text><Tag color={current ? 'success' : 'warning'}>{current ? '记录均为当前版本' : '记录尚未齐全或已过期'}</Tag></Space>
    <ul>{records.map(item => <li key={item.record_id}>{item.role === 'prepared' ? '编制' : '复核'}：{item.actor.display_name} · 本机姓名 · {new Date(item.created_at).toLocaleString('zh-CN')}
      {' · '}{item.content_revision === contentRevision && item.project_context_revision === contextRevision ? '当前内容' : '旧内容记录'}{item.note && ` · ${item.note}`}</li>)}</ul>
    <Input.TextArea aria-label={`${kind}编制复核说明`} placeholder="本次编制或复核说明" value={note} onChange={event => setNote(event.target.value)} rows={2} disabled={writer.blocked} />
    <Space className="application-message" wrap><Button disabled={writer.blocked} onClick={() => submit('prepared')}>登记编制</Button>
      <Button disabled={writer.blocked || !prepared} onClick={() => submit('reviewed')}>登记复核</Button>
      {!prepared && <Text type="secondary">先登记当前内容的编制，再登记复核。</Text>}</Space></div>
}
function ProjectEditor({ project, writer, createPath }: { project?: AuditProject; writer: Writer; createPath: string }) {
  const [form] = Form.useForm<AuditProjectFields>()
  useEffect(() => {
    const draft = writer.pending?.path === (project ? auditProjectPath(project.project_id) : createPath) && writer.pending.payload
    form.setFieldsValue(draft && 'project' in draft ? draft.project : project?.project || projectFields)
  }, [project?.project_id, form])
  return <Form form={form} layout="vertical" disabled={writer.blocked} onFinish={fields => writer.submit(
    project ? auditProjectPath(project.project_id) : createPath,
    { ...auditCommand(writer.actor), ...(project ? { expected_revision: project.revision } : {}), project: fields },
    project ? '保存项目目标' : '创建审计项目')}>
    <div className="audit-form-grid"><Form.Item label="项目名称" name="title" rules={[{ required: true, whitespace: true, message: '填写项目名称' }, { max: 200 }]}><Input aria-label="项目名称" /></Form.Item>
      <Form.Item label="审计对象" name="audit_object" rules={[{ required: true, whitespace: true, message: '填写审计对象' }, { max: 200 }]}><Input aria-label="审计对象" /></Form.Item>
      <Form.Item label="期间开始" name="period_start" rules={[{ required: true, message: '选择期间开始' }]}><Input type="date" aria-label="期间开始" /></Form.Item>
      <Form.Item label="期间结束" name="period_end" dependencies={['period_start']} rules={[{ required: true, message: '选择期间结束' },
        ({ getFieldValue }) => ({ validator: (_, value) => !value || value >= getFieldValue('period_start') ? Promise.resolve() : Promise.reject(new Error('期间结束不能早于开始')) })]}><Input type="date" aria-label="期间结束" /></Form.Item>
      <Form.Item className="audit-wide" label="审计目标" name="objective" rules={[{ max: 4000 }]}><Input.TextArea aria-label="审计目标" rows={3} /></Form.Item></div>
    <Button type="primary" htmlType="submit">{project ? '保存项目目标' : '创建审计项目'}</Button>
  </Form>
}
function ProcedureEditor({ project, procedure, batches, writer, workspaceId }: {
  project: AuditProject; procedure?: AuditProcedure; batches: Batch[]; writer: Writer; workspaceId: string
}) {
  const [form] = Form.useForm<AuditProcedureFields>()
  const [selection, setSelection] = useState<string[]>(() => procedure?.batches.map(item => `${item.batch_id}:${item.batch_revision}`) || [])
  useEffect(() => {
    const editPath = `${auditProjectPath(project.project_id)}/procedures${procedure ? '/' + encodeURIComponent(procedure.procedure_id) : ''}`
    const draft = writer.pending?.path === editPath && writer.pending.payload
    form.setFieldsValue(draft && 'procedure' in draft ? draft.procedure : procedure?.procedure || procedureFields)
    setSelection(draft && 'batches' in draft ? draft.batches.map(item => `${item.batch_id}:${item.batch_revision}`) : procedure?.batches.map(item => `${item.batch_id}:${item.batch_revision}`) || [])
  }, [procedure?.procedure_id, form])
  const options = batches.map(batch => ({ value: `${batch.batch_id}:${batch.revision}`, label: `${batch.template.name} · ${scenarioNames[batch.template.scenario]} · 批次版本 ${batch.revision}` }))
  for (const old of procedure?.batches || []) if (!options.some(item => item.value === `${old.batch_id}:${old.batch_revision}`)) options.push({
    value: `${old.batch_id}:${old.batch_revision}`, label: `${old.batch_id} · 已绑定旧版本 ${old.batch_revision}（可明确重新选择）`,
  })
  return <><Form form={form} layout="vertical" disabled={writer.blocked} onFinish={fields => writer.submit(
    `${auditProjectPath(project.project_id)}/procedures${procedure ? '/' + encodeURIComponent(procedure.procedure_id) : ''}`,
    { ...auditCommand(writer.actor), expected_revision: project.revision, procedure: fields,
      batches: selection.map(value => { const [batch_id, revision] = value.split(':'); return { batch_id, batch_revision: Number(revision) } }) },
    procedure ? '保存审计程序' : '新增审计程序')}>
    <Form.Item label="程序名称" name="title" rules={[{ required: true, whitespace: true, message: '填写程序名称' }, { max: 200 }]}><Input aria-label="程序名称" /></Form.Item>
    <div className="audit-form-grid"><Form.Item label="审计依据" name="basis" rules={[{ max: 4000 }]}><Input.TextArea aria-label="审计依据" rows={3} /></Form.Item>
      <Form.Item label="程序范围" name="scope" rules={[{ max: 4000 }]}><Input.TextArea aria-label="程序范围" rows={3} /></Form.Item></div>
    <Form.Item label="实际处理批次"><Select mode="multiple" aria-label="实际处理批次" value={selection} onChange={setSelection} options={options} placeholder="明确选择批次及其固定版本" /></Form.Item>
    <Form.Item label="程序说明" name="note" rules={[{ max: 4000 }]}><Input.TextArea aria-label="程序说明" rows={2} /></Form.Item>
    <Form.Item label="程序结论" name="conclusion" rules={[{ max: 4000 }]}><Input.TextArea aria-label="程序结论" rows={2} /></Form.Item>
    <Button type="primary" htmlType="submit">{procedure ? '保存审计程序' : '新增审计程序'}</Button></Form>
    {procedure && <><Paragraph className="application-message">已绑定 {procedure.batches.length} 个实际批次。打开批次可继续比较、原生校对与平台复核。</Paragraph><Space wrap>{procedure.batches.map(batch => <Link key={batch.batch_id}
      to={`/batches?workspace=${encodeURIComponent(workspaceId)}&batch=${encodeURIComponent(batch.batch_id)}`}>打开处理批次 {batch.batch_id} · 固定版本 {batch.batch_revision}</Link>)}</Space>
      <RecordControls kind="procedure" id={procedure.procedure_id} contentRevision={procedure.content_revision} contextRevision={procedure.project_context_revision}
        records={procedure.records} current={procedure.records_current} project={project} writer={writer} /></>}</>
}
function IssuePicker({ procedure, workspaceId, writer, selected, onSelected }: {
  procedure: AuditProcedure; workspaceId: string; writer: Writer; selected: AuditEvidenceSelection[]; onSelected: (value: AuditEvidenceSelection[]) => void
}) {
  const [batchId, setBatchId] = useState(procedure.batches[0]?.batch_id || '')
  const [page, setPage] = useState(1)
  const [filter, setFilter] = useState('all')
  useEffect(() => { setPage(1) }, [batchId, filter])
  const issues = useQuery({ queryKey: ['audit', workspaceId, 'issues', batchId, page, filter], enabled: !!batchId,
    queryFn: ({ signal }) => request<IssuePage>(`/batches/${encodeURIComponent(batchId)}/issues?offset=${(page - 1) * PAGE_SIZE}&limit=${PAGE_SIZE}${filter === 'all' ? '' : '&disposition=' + filter}`, { signal }) })
  return <section className="audit-records"><div className="section-head"><Title level={3}>从批次事项选择调查证据</Title><Link to={`/batches?workspace=${encodeURIComponent(workspaceId)}${batchId ? '&batch=' + encodeURIComponent(batchId) : ''}`}>打开处理批次</Link></div>
    <Space wrap className="audit-selection"><Select aria-label="调查来源批次" value={batchId || undefined} onChange={setBatchId} disabled={writer.blocked}
      placeholder="先为审计程序绑定批次" style={{ minWidth: 240 }} options={procedure.batches.map(batch => ({ value: batch.batch_id, label: `${batch.batch_id} · 程序绑定版本 ${batch.batch_revision}` }))} />
      <Select aria-label="调查来源处置筛选" value={filter} onChange={setFilter} options={[{ value: 'all', label: '全部事项' }, { value: 'pending', label: '待处理' }, { value: 'confirmed', label: '已确认' }, { value: 'deferred', label: '暂缓' }]} />
      <Text>明确选中 {selected.length} 项 · 当前批次所见版本 {issues.data?.revision ?? '…'}</Text></Space>
    <ReadError error={issues.error} refresh={() => void issues.refetch()} />
    <Table<Issue> size="small" rowKey="item_id" dataSource={issues.data?.items || []} pagination={false} loading={!!batchId && issues.isPending}
      rowSelection={{ selectedRowKeys: selected.filter(item => item.batch_id === batchId).map(item => item.item_id),
        getCheckboxProps: () => ({ disabled: writer.blocked || !issues.data }), onChange: keys => {
          const pageIds = new Set(issues.data?.items.map(item => item.item_id))
          onSelected([...selected.filter(item => item.batch_id !== batchId || !pageIds.has(item.item_id)),
            ...keys.map(String).filter(item_id => pageIds.has(item_id)).map(item_id => ({ batch_id: batchId, item_id, batch_revision: issues.data!.revision }))])
        } }} columns={[{ title: '事项', dataIndex: 'title' }, { title: '批次复核状态', dataIndex: 'disposition', render: value => stateNames[value] || value },
        { title: '详情', dataIndex: 'detail' }]} locale={{ emptyText: '所选批次当前页暂无事项' }} />
    <Pagination className="application-message" current={page} pageSize={PAGE_SIZE} total={issues.data?.total || 0} onChange={setPage} showSizeChanger={false} />
    <Paragraph type="secondary">只保存明确选择的事项，跨页选择保留；保存时固定每项所见批次版本和原始证据快照。</Paragraph>
    {!!selected.length && <Button disabled={writer.blocked} onClick={() => onSelected([])}>清除本次证据选择</Button>}
  </section>
}
function FindingEditor({ finding, procedure, project, workspaceId, writer }: {
  finding?: AuditFinding; procedure: AuditProcedure; project: AuditProject; workspaceId: string; writer: Writer
}) {
  const [form] = Form.useForm<AuditFindingFields>()
  const [selected, setSelected] = useState<AuditEvidenceSelection[]>([])
  const [forensic, setForensic] = useState<ForensicEvidenceSelection[]>([])
  const [kept, setKept] = useState<(AuditEvidenceRef | ForensicEvidenceRef)[]>(finding?.evidence_refs || [])
  useEffect(() => {
    const editPath = `${auditProjectPath(project.project_id)}/findings${finding ? '/' + encodeURIComponent(finding.finding_id) : ''}`
    const draft = writer.pending?.path === editPath && writer.pending.payload
    form.setFieldsValue(draft && 'finding' in draft ? draft.finding : finding?.finding || findingFields)
    setSelected(draft && 'evidence' in draft ? draft.evidence.filter((item): item is AuditEvidenceSelection => 'batch_id' in item) : [])
    setForensic(draft && 'evidence' in draft ? draft.evidence.filter((item): item is ForensicEvidenceSelection => 'inspection_id' in item) : [])
    setKept(draft && 'evidence' in draft ? (finding?.evidence_refs || []).filter(item => draft.evidence.some(value => 'evidence_id' in value && value.evidence_id === item.evidence_id)) : finding?.evidence_refs || [])
  }, [finding?.finding_id, form])
  return <><Form form={form} layout="vertical" disabled={writer.blocked} onFinish={fields => writer.submit(
    `${auditProjectPath(project.project_id)}/findings${finding ? '/' + encodeURIComponent(finding.finding_id) : ''}`,
    { ...auditCommand(writer.actor), expected_revision: project.revision, ...(!finding ? { procedure_id: procedure.procedure_id } : {}),
      finding: fields, evidence: [...kept.map(item => ({ evidence_id: item.evidence_id })), ...selected, ...forensic] }, finding ? '保存调查说明与结论' : '生成调查事项')}>
    <Form.Item label="调查事项名称" name="title" rules={[{ required: true, whitespace: true, message: '填写调查事项名称' }, { max: 200 }]}><Input aria-label="调查事项名称" /></Form.Item>
    <div className="audit-form-grid"><Form.Item label="优先级" name="priority"><Select aria-label="调查优先级" options={Object.entries(priorityNames).map(([value, label]) => ({ value, label }))} /></Form.Item>
      <Form.Item label="调查状态" name="status"><Select aria-label="调查状态" options={[{ value: 'draft', label: '尚未解决' }, { value: 'resolved', label: '已解决' }]} /></Form.Item></div>
    <Form.Item label="调查解释" name="explanation" rules={[{ max: 4000 }]}><Input.TextArea aria-label="调查解释" rows={3} /></Form.Item>
    <Form.Item label="调查结论" name="conclusion" rules={[{ max: 4000 }]}><Input.TextArea aria-label="调查结论" rows={3} /></Form.Item>
    {!!kept.length && <><Text strong>已固定的证据引用</Text><ul className="audit-evidence-list">{kept.map(item => <li key={item.evidence_id}>{'inspection_id' in item ? `图像取证 ${item.inspection_id} · 审阅版本 ${item.inspection_revision}` : `${item.item_id} · 批次 ${item.batch_id} 版本 ${item.batch_revision}`}
      {' · '}来源 {item.source_count} 处 <Space wrap>{item.sources.map((source, index) => <Link key={index} to={`/assets?workspace=${encodeURIComponent(workspaceId)}&asset=${encodeURIComponent(source.asset_id)}`}>查看原件{index + 1}</Link>)}</Space>
      {' '}<Button size="small" onClick={() => setKept(values => values.filter(value => value.evidence_id !== item.evidence_id))}>取消引用</Button></li>)}</ul></>}
    <IssuePicker key={procedure.procedure_id} procedure={procedure} workspaceId={workspaceId} writer={writer} selected={selected} onSelected={setSelected} />
    <ForensicPicker workspaceId={workspaceId} disabled={writer.blocked} selected={forensic} onSelected={setForensic} />
    <Button className="application-message" type="primary" htmlType="submit" disabled={!kept.length && !selected.length && !forensic.length}>{finding ? '保存调查说明与结论' : '生成调查事项'}</Button>
  </Form>{finding && <RecordControls kind="finding" id={finding.finding_id} contentRevision={finding.content_revision} contextRevision={finding.project_context_revision}
    records={finding.records} current={finding.records_current} project={project} writer={writer} />}</>
}
function Workpapers({ project, workspaceId, writer }: { project: AuditProject; workspaceId: string; writer: Writer }) {
  const [reviewIds, setReviewIds] = useState<string[]>([])
  const artifacts = useQuery({ queryKey: ['audit', workspaceId, 'artifacts', project.revision], queryFn: ({ signal }) => auditArtifacts(workspaceId, signal) })
  const reviews = (artifacts.data || []).filter((item): item is ReviewBundle => item.kind === 'batch_review')
  const workpapers = (artifacts.data || []).filter((item): item is WorkpaperBundle => item.kind === 'audit_workpaper' && item.project_id === project.project_id)
  const eligible = reviews.filter(item => item.status === 'current' && item.delivery_kind === 'final')
  const generate = (kind: 'intermediate' | 'final') => writer.submit(`${auditProjectPath(project.project_id)}/workpapers`, {
    ...auditCommand(writer.actor), expected_revision: project.revision, kind, review_bundle_ids: [...reviewIds],
  }, kind === 'final' ? '生成最终审计底稿' : '生成中间审计底稿')
  return <><Title level={3}>生成审计底稿</Title><Paragraph>先保存项目、程序与调查结论，并登记各自当前版本的编制和复核。最终底稿会核实全部调查已解决，以及明确选择的批次最终复核成果。</Paragraph>
    <ReadError error={artifacts.error} refresh={() => void artifacts.refetch()} />
    <Form layout="vertical"><Form.Item label="明确采用的批次最终复核成果"><Select mode="multiple" aria-label="底稿采用复核成果" value={reviewIds} onChange={setReviewIds}
      disabled={writer.blocked} loading={artifacts.isPending} options={eligible.map(item => ({ value: item.bundle_id, label: `${item.title} · 批次 ${item.batch_id} 版本 ${item.batch_revision}` }))} /></Form.Item></Form>
    {!artifacts.isPending && !eligible.length && <Paragraph>当前没有批次最终复核成果。纯图像取证项目采用已审阅的固定证据，由后端核实底稿条件。<Link to={`/batches?workspace=${encodeURIComponent(workspaceId)}`}>进入处理批次完成复核并生成成果</Link>。</Paragraph>}
    <Space wrap><Button disabled={writer.blocked} onClick={() => generate('intermediate')}>生成中间审计底稿</Button>
      <Button type="primary" disabled={writer.blocked || reviewIds.some(id => !eligible.some(item => item.bundle_id === id))} onClick={() => generate('final')}>生成最终审计底稿</Button>
      <Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}`}>打开成果中心</Link></Space>
    {!workpapers.length && <Empty description="当前项目尚无审计底稿" />}
    {workpapers.map(item => <article key={item.workpaper_id} className="audit-workpaper"><Space wrap><Title level={3}>{item.title}</Title>
      <Tag color={item.delivery_kind === 'final' ? 'success' : 'warning'}>{item.delivery_kind === 'final' ? '最终底稿' : '中间底稿'}</Tag>
      <Tag color={item.status === 'current' ? 'success' : 'warning'}>{item.status === 'current' ? '当前版本' : '旧底稿'}</Tag></Space>
      <Paragraph>固定项目版本 {item.project_revision} · 未解决 {item.unresolved_count} 项 · {new Date(item.created_at).toLocaleString('zh-CN')}</Paragraph>
      {item.status === 'outdated' && <Alert type="warning" title="项目已变化，此底稿为旧版本" description="固定文件仍可下载；请在成果中心核对前后继和采用关系。" />}
      {!!item.unresolved_count && <Alert type="warning" title={`仍有 ${item.unresolved_count} 项未解决`} description={item.unresolved_reasons.join('；')} />}
      <Space className="application-message" wrap>{item.files.map(file => file.availability === 'available' ? <a key={file.name}
        href={`${API}/workspaces/${encodeURIComponent(workspaceId)}/artifacts/${encodeURIComponent(item.bundle_id)}/files/${encodeURIComponent(file.name)}`}>下载 {file.name}</a> : <Tag key={file.name} color="error">{file.name} 不可用</Tag>)}
        <Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}&bundle=${encodeURIComponent(item.bundle_id)}`}>查看底稿与前后继</Link></Space></article>)}</>
}
function AuditWorkspace({ workspaceId }: { workspaceId: string }) {
  const [params, setParams] = useSearchParams()
  const projectId = params.get('project') || ''
  const procedureId = params.get('procedure') || ''
  const findingId = params.get('finding') || ''
  const [createProject, setCreateProject] = useState(false)
  const [createProcedure, setCreateProcedure] = useState(false)
  const [createFinding, setCreateFinding] = useState(false)
  const [tab, setTab] = useState('project')
  const [actor, setActor] = useState(localReviewer)
  const [pending, setPending] = useState(() => pendingAudit(workspaceId))
  const [notice, setNotice] = useState('')
  const client = useQueryClient()
  const base = `/workspaces/${encodeURIComponent(workspaceId)}/audit-projects`
  const projects = useAuditPage<AuditProjectPage>(workspaceId, base)
  const project = useQuery({ queryKey: ['audit', workspaceId, 'project', projectId], enabled: !!projectId,
    queryFn: ({ signal }) => request<AuditProject>(auditProjectPath(projectId), { signal }) })
  const batches = useQuery({ queryKey: ['audit', workspaceId, 'batches'], queryFn: ({ signal }) => request<{ items: Batch[] }>(`/batches?workspace_id=${encodeURIComponent(workspaceId)}`, { signal }) })
  function navigate(values: { project?: string; procedure?: string; finding?: string }) {
    const next = new URLSearchParams(params)
    for (const name of ['project', 'procedure', 'finding']) { const value = values[name as keyof typeof values]; if (value) next.set(name, value); else next.delete(name) }
    setParams(next)
  }
  const remember = (value: PendingAudit | null) => { persistAudit(workspaceId, value); setPending(value) }
  async function refresh() {
    await Promise.all([client.invalidateQueries({ queryKey: ['audit', workspaceId] }),
      client.invalidateQueries({ queryKey: ['todos', workspaceId] }),
      client.invalidateQueries({ queryKey: ['artifact-bundles', workspaceId] }), client.invalidateQueries({ queryKey: ['artifact-bundle', workspaceId] })])
  }
  async function committed(receipt: AuditReceipt) {
    remember(null); setNotice('已保存；当前项目内容已刷新。'); setCreateProject(false)
    if ('entity_kind' in receipt) {
      if (receipt.entity_kind === 'project') navigate({ project: receipt.project_id })
      if (receipt.entity_kind === 'procedure') { setCreateProcedure(false); navigate({ project: receipt.project_id, procedure: receipt.entity_id }); setTab('procedures') }
      if (receipt.entity_kind === 'finding') {
        const latest = await request<AuditFinding>(`${auditProjectPath(receipt.project_id)}/findings/${encodeURIComponent(receipt.entity_id)}`)
        setCreateFinding(false); navigate({ project: receipt.project_id, procedure: latest.procedure_id, finding: receipt.entity_id }); setTab('findings')
      }
    } else { setTab('workpapers'); setNotice(receipt.delivery_kind === 'final' ? '最终底稿已生成，可下载两个固定文件。' : '中间底稿已生成，未解决条件随底稿保留。') }
    await refresh()
  }
  const save = useMutation({ mutationFn: replayAudit, onSuccess: committed,
    onError: (error, value) => { remember(pendingFailure(error, value)); setNotice('') } })
  const recovery = useMutation({ mutationFn: async (release: boolean) => {
    if (!pending) throw new Error('没有待核实请求')
    if (pending.project_id) {
      try {
        const status = await auditRequestStatus(pending)
        if (status.state === 'committed' && status.receipt) return status.receipt
        if (!release && status.state === 'publishing' && pending.path.endsWith('/workpapers')) return replayAudit(pending)
        throw new Error('原请求仍在发布或待核实，请继续核实同一请求。')
      } catch (error) {
        if (!(error instanceof HttpError) || error.status !== 404) throw error
        if (release && pending.status !== 'uncertain') {
          const latest = await request<AuditProject>(auditProjectPath(pending.project_id))
          client.setQueryData(['audit', workspaceId, 'project', pending.project_id], latest)
          return null
        }
      }
    } else if (release && pending.status === 'rejected') return null
    return replayAudit(pending)
  }, onSuccess: async receipt => { if (receipt) await committed(receipt); else { remember(null); save.reset(); setNotice('原请求未提交；已刷新所见版本并保留草稿，请核对后再次保存。'); await refresh() } },
    onError: error => setNotice(error.message) })
  const writer: Writer = { actor, pending, blocked: !actor.display_name.trim() || !!pending || save.isPending || recovery.isPending,
    submit: (path, payload, label) => { if (!actor.display_name.trim() || pending || save.isPending || recovery.isPending) return
      const value: PendingAudit = { workspace_id: workspaceId, ...(projectId && path !== base ? { project_id: projectId } : {}), path, payload, label, status: 'uncertain' }
      remember(value); save.mutate(value)
    } }
  return <><section className="panel audit-panel"><Space wrap><Text strong>本机操作人</Text><Input aria-label="审计本机操作人" value={actor.display_name} maxLength={100} style={{ width: 220 }}
    disabled={!!pending || save.isPending || recovery.isPending} placeholder="明确填写编制 / 复核姓名" onChange={event => { const value = { ...actor, display_name: event.target.value }; setActor(value); localStorage.setItem('clear-local-reviewer', JSON.stringify(value)) }} />
    <Text type="secondary">姓名来源：本机填写</Text><Button onClick={() => void refresh()}>刷新项目与成果</Button></Space>
    {pending && <Alert className="application-message" type="warning" title={pending.status === 'conflict' ? '所见版本或发布条件冲突，原请求与草稿已保留' : pending.status === 'rejected' ? '原请求未被接受，草稿已保留' : '原请求响应需要核实'}
      description={<><Paragraph>{pending.label} · 请求 {pending.payload.request_id}{'expected_revision' in pending.payload ? ` · 所见项目版本 ${pending.payload.expected_revision}` : ''}</Paragraph>
        {pending.reason && <Paragraph>{pending.reason}</Paragraph>}<Space wrap><Button loading={recovery.isPending} disabled={save.isPending} onClick={() => recovery.mutate(false)}>核实原请求并继续</Button>
          {pending.status !== 'uncertain' && <Button disabled={save.isPending || recovery.isPending} onClick={() => recovery.mutate(true)}>核实并刷新所见版本</Button>}</Space></>} />}
    {notice && <Alert className="application-message" type={recovery.isError ? 'warning' : 'info'} title={notice} />}</section>
    {!projectId ? <section className="panel audit-panel"><div className="section-head"><Title level={3}>审计项目</Title><Button onClick={() => setCreateProject(value => !value)} disabled={!!pending}>新建项目</Button></div>
      {createProject && <ProjectEditor key="new-project" writer={writer} createPath={base} />}
      <ReadError error={projects.error} refresh={() => { projects.reset(); void projects.refetch() }} />
      <Table<AuditProject> rowKey="project_id" size="small" dataSource={projects.data?.items || []} loading={projects.isPending} pagination={false}
        columns={[{ title: '项目', key: 'title', render: (_, item) => <Button type="link" onClick={() => { navigate({ project: item.project_id }); setTab('project') }}>{item.project.title}</Button> },
          { title: '审计对象', key: 'object', render: (_, item) => item.project.audit_object }, { title: '期间', key: 'period', render: (_, item) => `${item.project.period_start} 至 ${item.project.period_end}` },
          { title: '程序 / 调查', key: 'counts', render: (_, item) => `${item.procedure_count} / ${item.finding_count}` }, { title: '版本', dataIndex: 'revision' }]} />
      <PageNavigation page={projects} /></section> : <><Space className="audit-selection" wrap><Link to={`/audit?workspace=${encodeURIComponent(workspaceId)}`}>返回全部审计项目</Link>
        <Link to={`/batches?workspace=${encodeURIComponent(workspaceId)}`}>打开处理批次</Link></Space>
      <ReadError error={project.error} refresh={() => void project.refetch()} />
      {project.isPending && <Paragraph>正在读取审计项目…</Paragraph>}
      {project.data && (project.data.workspace_id !== workspaceId ? <Alert type="error" title="此项目属于其他工作空间，请重新选择项目" /> :
        <ProjectWorkspace key={projectId} project={project.data} workspaceId={workspaceId} writer={writer} batches={batches.data?.items || []}
          tab={tab} setTab={setTab} procedureId={procedureId} findingId={findingId} navigate={navigate}
          createProcedure={createProcedure} setCreateProcedure={setCreateProcedure} createFinding={createFinding} setCreateFinding={setCreateFinding} />)}</>}
    <ReadError error={batches.error} refresh={() => void batches.refetch()} />
  </>
}
function ProjectWorkspace({ project, workspaceId, writer, batches, tab, setTab, procedureId, findingId, navigate, createProcedure, setCreateProcedure, createFinding, setCreateFinding }: {
  project: AuditProject; workspaceId: string; writer: Writer; batches: Batch[]; tab: string; setTab: (value: string) => void;
  procedureId: string; findingId: string; navigate: (values: { project?: string; procedure?: string; finding?: string }) => void;
  createProcedure: boolean; setCreateProcedure: (value: boolean) => void; createFinding: boolean; setCreateFinding: (value: boolean) => void
}) {
  const path = auditProjectPath(project.project_id)
  const procedures = useAuditPage<AuditProcedurePage>(workspaceId, `${path}/procedures`, project.revision)
  const findings = useAuditPage<AuditFindingPage>(workspaceId, `${path}/findings${procedureId ? '?procedure_id=' + encodeURIComponent(procedureId) : ''}`, project.revision)
  const procedure = useQuery({ queryKey: ['audit', workspaceId, path, 'procedure', procedureId, project.revision], enabled: !!procedureId,
    queryFn: ({ signal }) => request<AuditProcedure>(`${path}/procedures/${encodeURIComponent(procedureId)}`, { signal }) })
  const finding = useQuery({ queryKey: ['audit', workspaceId, path, 'finding', findingId, project.revision], enabled: !!findingId,
    queryFn: ({ signal }) => request<AuditFinding>(`${path}/findings/${encodeURIComponent(findingId)}`, { signal }) })
  useEffect(() => { if (findingId) setTab('findings'); else if (procedureId) setTab('procedures') }, [findingId, procedureId])
  return <section className="panel audit-panel"><div className="section-head"><div><Title level={2}>{project.project.title}</Title><Text type="secondary">所见项目版本 {project.revision} · {project.procedure_count} 个程序 · {project.finding_count} 个调查事项</Text></div>
    <Tag color={project.records_current ? 'success' : 'warning'}>{project.records_current ? '项目记录当前' : '项目待编制 / 复核'}</Tag></div>
    <Tabs activeKey={tab} onChange={setTab} items={[
      { key: 'project', label: '项目目标', children: <><ProjectEditor key={project.project_id} project={project} writer={writer} createPath="" />
        <RecordControls kind="project" id={project.project_id} contentRevision={project.content_revision} contextRevision={project.project_context_revision}
          records={project.records} current={project.records_current} project={project} writer={writer} /></> },
      { key: 'procedures', label: '审计程序', children: <><div className="section-head"><Title level={3}>程序与实际批次</Title><Button disabled={!!writer.blocked} onClick={() => { setCreateProcedure(true); navigate({ project: project.project_id }); setTab('procedures') }}>新增程序</Button></div>
        <ReadError error={procedures.error} refresh={() => { procedures.reset(); void procedures.refetch() }} /><Table<AuditProcedure> size="small" rowKey="procedure_id" dataSource={procedures.data?.items || []} pagination={false} loading={procedures.isPending}
          columns={[{ title: '程序', key: 'title', render: (_, item) => <Button type="link" onClick={() => { setCreateProcedure(false); navigate({ project: project.project_id, procedure: item.procedure_id }) }}>{item.procedure.title}</Button> },
            { title: '关联批次', key: 'batches', render: (_, item) => item.batches.length }, { title: '编制复核', key: 'records', render: (_, item) => item.records_current ? '当前' : '待完成 / 旧记录' }]} />
        <PageNavigation page={procedures} />
        <ReadError error={procedure.error} refresh={() => void procedure.refetch()} />
        {(createProcedure || procedure.data) && <div className="audit-records"><Title level={3}>{createProcedure ? '新增审计程序' : '编辑程序'}</Title>
          <ProcedureEditor key={createProcedure ? 'new-procedure' : procedureId} project={project} procedure={createProcedure ? undefined : procedure.data} batches={batches} writer={writer} workspaceId={workspaceId} />
          {!createProcedure && procedure.data && <Button className="application-message" onClick={() => { setCreateFinding(true); setTab('findings'); navigate({ project: project.project_id, procedure: procedureId }) }}>从程序生成调查事项</Button>}</div>}</> },
      { key: 'findings', label: '调查事项', children: <><div className="section-head"><Title level={3}>{procedure.data ? `${procedure.data.procedure.title} · 调查事项` : '项目调查事项'}</Title>
        {procedure.data && <Button disabled={writer.blocked} onClick={() => { setCreateFinding(true); navigate({ project: project.project_id, procedure: procedureId }); setTab('findings') }}>新增调查事项</Button>}</div>
        <ReadError error={findings.error} refresh={() => { findings.reset(); void findings.refetch() }} /><Table<AuditFinding> size="small" rowKey="finding_id" dataSource={findings.data?.items || []} loading={findings.isPending} pagination={false}
          columns={[{ title: '调查事项', key: 'title', render: (_, item) => <Button type="link" onClick={() => { setCreateFinding(false); navigate({ project: project.project_id, procedure: item.procedure_id, finding: item.finding_id }) }}>{item.finding.title}</Button> },
            { title: '优先级', key: 'priority', render: (_, item) => priorityNames[item.finding.priority || 'normal'] }, { title: '状态', key: 'status', render: (_, item) => <Tag color={item.finding.status === 'resolved' ? 'success' : 'warning'}>{item.finding.status === 'resolved' ? '已解决' : '尚未解决'}</Tag> },
            { title: '证据', key: 'evidence', render: (_, item) => item.evidence_refs.length }, { title: '编制复核', key: 'records', render: (_, item) => item.records_current ? '当前' : '待完成 / 旧记录' }]} />
        <PageNavigation page={findings} /><ReadError error={finding.error} refresh={() => void finding.refetch()} />
        {!procedure.data && <Paragraph className="application-message">先在“审计程序”选择程序，并绑定实际批次，再选择证据生成调查事项。</Paragraph>}
        {procedure.data && (createFinding || finding.data) && <div className="audit-records"><Title level={3}>{createFinding ? '生成调查事项' : '调查解释与结论'}</Title>
          <FindingEditor key={createFinding ? `new-finding-${procedureId}` : findingId} finding={createFinding ? undefined : finding.data} procedure={procedure.data} project={project} workspaceId={workspaceId} writer={writer} /></div>}</> },
      { key: 'workpapers', label: '审计底稿', children: <Workpapers project={project} workspaceId={workspaceId} writer={writer} /> },
    ]} />
  </section>
}
export function AuditWorkbench() {
  const workspaceId = useWorkspaceId()
  const [params] = useSearchParams()
  const reviewReturn = nativeReviewReturn(params.get('return_to'), workspaceId)
  return <div className="audit-workbench"><div className="page-heading"><Text className="eyebrow">AUDIT / PROJECTS</Text><Title level={1}>审计项目</Title>
    <Paragraph>明确审计对象和目标，绑定实际处理批次，形成调查结论，完成编制复核并交付固定版本底稿。</Paragraph></div>
    {reviewReturn && <Paragraph><Link to={reviewReturn}>返回原待办事项</Link></Paragraph>}
    {workspaceId ? <AuditWorkspace key={workspaceId} workspaceId={workspaceId} /> : <Empty description="先在顶部选择工作空间" />}</div>
}
