import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Pagination, Select, Space, Table, Tag, Typography } from 'antd'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import type { BatchItemRead, ItemEvidence, TodoLocate } from '../../../../../packages/contracts/types.generated'
import { API, HttpError, assets, fixedRequest, localReviewer, newRequestId, pendingRequest as restoredRequest, persistFixed, persistRequest, post, request, scenarioNames, stageLabel, stateNames, terminal, type Asset, type Batch, type BatchDisposition, type BatchHistoryPage, type BatchNativeRevisionPage, type BatchOperation, type FixedRequest, type Issue, type IssuePage, type LocalActor, type NativeAdoption, type Preflight, type ReviewArtifactRequest, type RunRequest, type Template } from './api'
import { TemplateSetup } from './TemplateSetup'
import { TemplateSummary } from './TemplateSummary'
import { UploadQueue } from './UploadQueue'
import { ItemEvidenceView } from '../todos/ItemEvidenceView'
import { nativeReviewReturn } from '../catalog/nativeReviewReturn'
import './batches.css'

const { Title, Text, Paragraph } = Typography
const valueNames: Record<string, string> = { category: '结果分类', label: '模型建议', predicted_label: '模型建议', predicted_label_id: '模型建议', prediction_label_id: '原模型建议', human_label_id: '人工标签', native_revision: '采用的原生版本', disposition_revision: '原生处置版本', revision: '修订版本', origin: '修订来源', edited: '人工校对内容', original: '原始识别内容', native_result_id: '采用结果身份', native_version_id: '采用原件版本', actor: '复核人', confidence: '分数', score: '分数', name: '名称', text: '识别文本', message: '信息', error: '错误', left: '左表', right: '右表', field: '字段', reason: '原因', page_number: '页码', source_row: '原行', left_source_row: '左表原行', right_source_row: '右表原行' }
function print(value: unknown): string { return typeof value === 'string' ? value : value === null ? '空值' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value) }
function Output({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <Paragraph type="secondary">此事项没有附加输出</Paragraph>
  if (typeof value !== 'object') return <Paragraph>{print(value)}</Paragraph>
  return <dl className="batch-output">{Object.entries(value).map(([key, item]) => <div key={key}><dt>{valueNames[key] || key}</dt><dd>{print(item)}</dd></div>)}</dl>
}
function Status({ value }: { value: string }) { return <Tag color={value === 'succeeded' || value === 'collected' ? 'success' : value.includes('failed') ? 'error' : value === 'running' || value === 'queued' ? 'processing' : 'default'}>{stateNames[value] || value}</Tag> }
const rejectedReviewRequests = ['复核成果版本冲突，请检查所见内容', '执行、全量覆盖或复核尚未完成，只能明确生成中间材料']
type ReviewPending = FixedRequest<ReviewArtifactRequest> & { rejection?: string }
function pendingPublication(batchId: string): ReviewPending | null {
  const value = fixedRequest<ReviewArtifactRequest>('review', batchId) as ReviewPending | null
  // A historical bare 409 status does not prove that no snapshot was created.
  return value?.status === 'conflict' && !rejectedReviewRequests.includes(value.rejection || '') ? { ...value, status: 'uncertain' } : value
}

function IssueQueue({ batch, nativeUrl, actor, writeBlocked, onBlocked, onMaterialReady }: { batch: Batch; nativeUrl: string; actor: LocalActor; writeBlocked: boolean; onBlocked: (value: boolean) => void; onMaterialReady: (value: boolean) => void }) {
  const client = useQueryClient()
  const [params, setParams] = useSearchParams()
  const navigate = useNavigate()
  const urlFilter = ['pending', 'confirmed', 'deferred', 'all'].includes(params.get('disposition') || '') ? params.get('disposition')! : 'pending'
  const requestedPage = Number(params.get('issue_page'))
  const urlPage = Number.isSafeInteger(requestedPage) && requestedPage > 0 ? requestedPage : 1
  const [filter, setFilter] = useState(urlFilter)
  const [page, setPage] = useState(urlPage)
  const [selected, setSelected] = useState<string[]>([])
  const [focused, setFocused] = useState<string | undefined>(params.get('item') || undefined)
  const [pending, setPending] = useState(() => fixedRequest<BatchDisposition>('disposition', batch.batch_id))
  const [note, setNote] = useState(() => pending?.payload.note || '')
  const [historyPage, setHistoryPage] = useState(1)
  const [refreshError, setRefreshError] = useState('')
  const classification = batch.template.scenario === 'classification'
  const nativeScenario = classification || batch.template.scenario === 'ocr'
  const operation = batch.summary.pending_operation
  useEffect(() => { setFilter(urlFilter); setPage(urlPage); setFocused(params.get('item') || undefined) }, [params.toString()])
  useEffect(() => { setSelected([]); if (!pending) setNote('') }, [filter, page, focused, batch.batch_id])
  function changeContext(value: { filter?: string; page?: number; item?: string | null }) {
    const next = new URLSearchParams(params)
    if (value.filter) { next.set('disposition', value.filter); setFilter(value.filter) }
    if (value.page) { next.set('issue_page', String(value.page)); setPage(value.page) }
    if ('item' in value) { if (value.item) next.set('item', value.item); else next.delete('item'); setFocused(value.item || undefined) }
    setParams(next, { replace: true })
  }
  const issues = useQuery({ queryKey: ['batch-issues', batch.batch_id, filter, page],
    queryFn: ({ signal }) => request<IssuePage>(`/batches/${encodeURIComponent(batch.batch_id)}/issues?offset=${(page - 1) * 20}&limit=20${filter === 'all' ? '' : `&disposition=${filter}`}`, { signal }),
    refetchInterval: terminal.includes(batch.status) ? false : 3000 })
  const path = `/batches/${encodeURIComponent(batch.batch_id)}`
  const onPage = issues.data?.items.find(item => item.item_id === focused)
  const direct = useQuery({ queryKey: ['batch-item', batch.batch_id, focused], enabled: !!focused && !onPage, refetchOnMount: 'always',
    queryFn: async ({ signal }) => {
      const value = await request<BatchItemRead>(`${path}/items/${encodeURIComponent(focused!)}`, { signal })
      if (value.workspace_id !== batch.workspace_id || value.batch_id !== batch.batch_id || value.item.item_id !== focused) throw new Error('事项响应与原上下文不一致')
      return value
    } })
  const current = focused ? onPage || (direct.data?.item as Issue | undefined) : issues.data?.items[0]
  const revision = focused && !onPage ? direct.data?.revision : issues.data?.revision
  const evidence = useQuery({ queryKey: ['batch-evidence', batch.batch_id, current?.item_id, revision], enabled: !!current && !!revision,
    refetchOnMount: 'always', queryFn: async ({ signal }) => {
      const value = await request<ItemEvidence>(`${path}/items/${encodeURIComponent(current!.item_id)}/evidence?batch_revision=${revision}`, { signal })
      if (!Array.isArray(value.fields) || value.workspace_id !== batch.workspace_id || value.batch_id !== batch.batch_id || value.item_id !== current!.item_id) throw new Error('证据响应与当前事项不一致')
      return value
    } })
  const nativeApplicable = nativeScenario && current?.category === 'review'
  const native = useQuery({ queryKey: ['batch-native-revisions', batch.batch_id, current?.item_id], enabled: nativeApplicable,
    refetchOnMount: 'always', queryFn: ({ signal }) => request<BatchNativeRevisionPage>(`${path}/items/${encodeURIComponent(current!.item_id)}/native-revisions?offset=0&limit=30`, { signal }) })
  useEffect(() => { setHistoryPage(1) }, [current?.item_id])
  const history = useQuery({ queryKey: ['batch-history', batch.batch_id, current?.item_id, historyPage], enabled: !!current,
    queryFn: ({ signal }) => request<BatchHistoryPage>(`${path}/history?item_id=${encodeURIComponent(current!.item_id)}&offset=${(historyPage - 1) * 30}&limit=30`, { signal }) })
  const operationQuery = useQuery({ queryKey: ['batch-operation', batch.batch_id, operation?.operation_id], enabled: !!operation,
    queryFn: ({ signal }) => request<BatchOperation>(`${path}/operations/${encodeURIComponent(operation!.operation_id)}`, { signal }) })
  function remember(value: FixedRequest<BatchDisposition> | null) { setPending(value); persistFixed('disposition', batch.batch_id, value) }
  async function refreshReads() {
    await Promise.all([client.invalidateQueries({ queryKey: ['batch-issues', batch.batch_id] }), client.invalidateQueries({ queryKey: ['batch', batch.batch_id] }),
      client.invalidateQueries({ queryKey: ['batch-history', batch.batch_id] }), client.invalidateQueries({ queryKey: ['batch-native-revisions', batch.batch_id] }),
      client.invalidateQueries({ queryKey: ['batch-item', batch.batch_id] }), client.invalidateQueries({ queryKey: ['batch-evidence', batch.batch_id] }), client.invalidateQueries({ queryKey: ['todos', batch.workspace_id] }),
      client.invalidateQueries({ queryKey: ['artifact-bundles', batch.workspace_id] }), client.invalidateQueries({ queryKey: ['artifact-bundle', batch.workspace_id] })])
  }
  const save = useMutation({ mutationFn: (payload: BatchDisposition) => post<Batch>(`${path}/dispositions`, payload),
    onSuccess: async () => {
      const records = issues.data?.items || []
      const index = records.findIndex(item => item.item_id === current?.item_id)
      remember(null); setNote(''); setSelected([])
      await refreshReads()
      const origin = nativeReviewReturn(params.get('return_to'), batch.workspace_id)
      if (origin?.startsWith('/todos?')) {
        const target = new URL(origin, 'http://127.0.0.1:1')
        target.searchParams.delete('cursor'); target.searchParams.delete('snapshot_token')
        const nextId = params.get('next_todo')
        if (nextId) {
          target.searchParams.set('todo', nextId)
          const query = new URLSearchParams({ limit: '20' })
          for (const name of ['kind', 'issue_type', 'scenario', 'disposition', 'finding_status', 'execution_state', 'project_id', 'procedure_id', 'priority', 'sort']) {
            const value = target.searchParams.get(name); if (value) query.set(name, value)
          }
          try {
            const located = await request<TodoLocate>(`/workspaces/${encodeURIComponent(batch.workspace_id)}/todos/${encodeURIComponent(nextId)}?${query}`)
            if (located.matches_filters) {
              if (located.page_cursor) target.searchParams.set('cursor', located.page_cursor)
              target.searchParams.set('snapshot_token', located.snapshot_token)
            }
          } catch { /* The committed write stays complete; the destination retries the selected identity. */ }
        } else target.searchParams.delete('todo')
        navigate(target.pathname + target.search)
      } else changeContext({ item: records[index + 1]?.item_id || null, ...(page > 1 && records.length === 1 && filter !== 'all' ? { page: page - 1 } : {}) })
    }, onError: (error, payload) => { remember({ payload, status: error instanceof HttpError && error.status === 409 ? 'conflict' : 'uncertain' }); void client.invalidateQueries({ queryKey: ['batch', batch.batch_id] }) } })
  const recovery = useMutation({ mutationFn: async () => {
    const fixed = await request<BatchOperation>(`${path}/operations/${encodeURIComponent(operation!.operation_id)}`)
    if (fixed.state === 'committed') {
      if (fixed.receipt.outcome === 'conflict') throw new HttpError(String(fixed.receipt.reason), 409)
      return request<Batch>(path)
    }
    return post<Batch>(`${path}/operations/${encodeURIComponent(fixed.operation_id)}/recover`, {})
  }, onSuccess: async () => {
    if (pending?.payload.request_id === operation?.request_id) { remember(null); setNote(''); setSelected([]) }
    await refreshReads()
  }, onError: () => { void operationQuery.refetch(); void client.invalidateQueries({ queryKey: ['batch', batch.batch_id] }) } })
  useEffect(() => { onBlocked(!!pending || !!operation || save.isPending || recovery.isPending) }, [pending, operation?.operation_id, save.isPending, recovery.isPending, onBlocked])
  const durableOperation = operationQuery.data
  const materialReady = !!durableOperation && !!operation && durableOperation.operation_id === operation.operation_id &&
    ['needs_verification', 'native_committed'].includes(durableOperation.state) && (!pending || pending.payload.request_id === durableOperation.request_id) && !save.isPending && !recovery.isPending
  useEffect(() => { onMaterialReady(materialReady) }, [materialReady, onMaterialReady])
  const disabled = !actor.display_name.trim() || !revision || !!pending || !!operation || save.isPending || recovery.isPending || writeBlocked || direct.isError && !onPage
  function submit(itemIds: string[], action: BatchDisposition['action'], adoption?: NativeAdoption) {
    if (disabled || !itemIds.length) return
    const expectedRevision = itemIds.length === 1 && itemIds[0] === current?.item_id ? revision! : issues.data!.revision
    const payload: BatchDisposition = { request_id: newRequestId(), expected_revision: adoption ? native.data!.revision : expectedRevision,
      actor: { ...actor, display_name: actor.display_name.trim() }, item_ids: [...itemIds], action, note, ...(adoption ? { adoption } : {}) }
    remember({ payload, status: 'uncertain' }); save.mutate(payload)
  }
  async function refreshConflict() {
    setRefreshError('')
    try {
      const latest = await request<Batch>(path)
      client.setQueryData(['batch', batch.batch_id], latest)
      await refreshReads()
      if (latest.summary.pending_operations) throw new Error('仍有原操作待核实，请先恢复原操作。')
      if (pending?.status === 'conflict') { remember(null); save.reset() }
    } catch (error) { setRefreshError((error as Error).message) }
  }
  const currentNative = native.data?.current
  const adoption: NativeAdoption | undefined = native.data?.adoptable ? classification
    ? typeof currentNative?.human_label_id === 'string' ? { native_revision: native.data.current_revision, human_label_id: currentNative.human_label_id } : undefined
    : typeof currentNative?.id === 'string' && typeof currentNative?.version_id === 'string' ? { native_revision: native.data.current_revision, native_result_id: currentNative.id, native_version_id: currentNative.version_id } : undefined : undefined
  return <section className="panel"><div className="section-head"><Title level={3}>异常与复核</Title>
    <Select aria-label="复核状态筛选" value={filter} onChange={value => changeContext({ filter: value, page: 1, item: null })} options={[{ value: 'pending', label: '待处理' }, { value: 'deferred', label: '暂缓' }, { value: 'confirmed', label: '已确认' }, { value: 'all', label: '全部记录' }]} /></div>
    <Paragraph type="secondary">原始输出与本次处置分别保留。暂缓仍属于待处理事项，技术执行成功不代表复核完成。</Paragraph>
    <Paragraph type="secondary">所见处置版本：{revision ?? '读取中'}。每次写入固定这个版本和请求身份。</Paragraph>
    {nativeReviewReturn(params.get('return_to'), batch.workspace_id) && <Paragraph><Link to={nativeReviewReturn(params.get('return_to'), batch.workspace_id)!}>返回原待办筛选与事项</Link></Paragraph>}
    {classification && <Paragraph type="secondary">确认表示接受本次原始模型建议。需要改标签时进入 <Link to={nativeUrl}>原生项目复核</Link>，返回后明确读取并采用人工修订。</Paragraph>}
    {batch.template.scenario === 'ocr' && <Paragraph type="secondary">平台只确认本次固定结果；OCR 原生复核资格独立，未表示原生复核通过。可进入 <Link to={nativeUrl}>当前原生 OCR 项目</Link>校对，返回后明确读取并采用修订。</Paragraph>}
    {pending && <Alert type="warning" title={pending.status === 'conflict' ? '所见版本冲突，说明和原请求已保留' : '原处置响应需要核实'}
      description={<><Paragraph>请求 {pending.payload.request_id} · 版本 {pending.payload.expected_revision} · {pending.payload.actor.display_name}。核实完成前不能创建替代请求。</Paragraph>
        <Paragraph>原固定处置：{{ confirm: '确认', defer: '暂缓', reopen: '重开', adopt: '采用修订' }[pending.payload.action]} · {pending.payload.item_ids.length} 项（{pending.payload.item_ids.join('、')}）；原说明：{pending.payload.note || '未填写'}</Paragraph><Space wrap>
        {!operation && <Button disabled={save.isPending} onClick={() => save.mutate(pending.payload)}>按原请求核实并重放</Button>}
        <Button disabled={save.isPending || recovery.isPending} onClick={() => void refreshConflict()}>{pending.status === 'conflict' ? '刷新核对并结束冲突请求' : '刷新核对，保留原请求'}</Button></Space></>} />}
    {operation && <Alert type="warning" title="原操作待核实" description={<><Paragraph>操作 {operation.operation_id} · 请求 {operation.request_id} · {operationQuery.data?.state || operation.state}。沿用原固定参数，恢复仅核实原操作。</Paragraph>
      {operationQuery.data && <Paragraph>原复核人：{operationQuery.data.actor.display_name} · 所见版本 {operationQuery.data.expected_revision}</Paragraph>}
      <Space><Button onClick={() => void operationQuery.refetch()}>查询原操作</Button><Button loading={recovery.isPending} onClick={() => recovery.mutate()}>恢复原操作</Button></Space></>} />}
    {operationQuery.isError && <Alert type="error" title={operationQuery.error.message} />}
    {recovery.isError && <Alert type="error" title={recovery.error.message} />}
    {refreshError && <Alert type="error" title={refreshError} />}
    {issues.isError && <Alert type="error" title={issues.error.message} action={<Button onClick={() => void issues.refetch()}>重试</Button>} />}
    {direct.isError && !onPage && <Alert type="error" title={direct.error.message} action={<Button onClick={() => void direct.refetch()}>按原事项身份重试</Button>} />}
    <div className="batch-review-layout"><div className="batch-queue">
      <div className="batch-selection"><Text>本页已明确选中 {selected.length} 项</Text><Button size="small" disabled={!selected.length} onClick={() => setSelected([])}>清除选中</Button></div>
      <Table<Issue> size="small" rowKey="item_id" dataSource={issues.data?.items || []} loading={issues.isPending} pagination={false}
        rowSelection={{ selectedRowKeys: selected, onChange: keys => setSelected(keys.map(String)) }}
        rowClassName={item => item.item_id === current?.item_id ? 'batch-active-issue' : ''}
        columns={[{ title: '事项', key: 'title', render: (_, item) => <Button type="link" className="batch-issue-link" onClick={() => { changeContext({ item: item.item_id }); if (!pending) setNote('') }}>{item.title}</Button> },
          { title: '处置', dataIndex: 'disposition', width: 100, render: value => <Status value={value} /> }]}
        locale={{ emptyText: <Empty description={issues.isPending ? '读取复核记录…' : '当前筛选没有事项'} /> }} />
      <div className="batch-pagination"><Pagination current={page} pageSize={20} total={issues.data?.total || 0} onChange={value => changeContext({ page: value, item: null })} showSizeChanger={false} size="small" />
        <Text type="secondary">全选只选择当前页，切页或切换筛选会清除选中。</Text></div>
      <Space wrap><Button disabled={!selected.length || disabled} onClick={() => submit(selected, 'confirm')}>{classification ? '确认选中建议' : '确认选中'} {selected.length} 项</Button>
        <Button disabled={!selected.length || disabled} onClick={() => submit(selected, 'defer')}>暂缓选中</Button>
        <Button disabled={!selected.length || disabled} onClick={() => submit(selected, 'reopen')}>重开选中</Button></Space>
    </div><div className="batch-review-item">
      {current ? <><div className="section-head"><Title level={4}>{current.title}</Title><Status value={current.disposition} /></div>
        <Paragraph>{current.detail}</Paragraph>
        {focused && !onPage && <Paragraph type="secondary">按原事项身份读取，不受当前列表页限制。原筛选和分页保持不变。</Paragraph>}
        {filter !== 'all' && current.disposition !== filter && <Alert type="info" title="此事项已不符合原筛选，仍保留当前事项上下文" />}
        {evidence.data && <ItemEvidenceView evidence={evidence.data} />}
        {evidence.isError && <Alert type="warning" title={evidence.error.message} action={<Button onClick={() => void evidence.refetch()}>刷新字段对照</Button>} />}
        <details><summary>批次技术详情 · 原始输出与来源记录</summary><Output value={current.source} /><Output value={current.raw_output} /></details>
        {current.note && <Paragraph type="secondary">上次说明：{current.note}</Paragraph>}
        <Input.TextArea aria-label="复核说明" placeholder="处置说明（可选）" rows={2} maxLength={2000} value={note} disabled={!!pending || save.isPending || recovery.isPending} onChange={event => setNote(event.target.value)} />
        <Space wrap className="batch-review-actions"><Button type="primary" disabled={disabled} loading={save.isPending} onClick={() => submit([current.item_id], 'confirm')}>{classification ? '确认模型建议并下一项' : '确认并下一项'}</Button>
          <Button disabled={disabled} onClick={() => submit([current.item_id], 'defer')}>暂缓并下一项</Button>
          <Button disabled={disabled || current.disposition === 'pending'} onClick={() => submit([current.item_id], 'reopen')}>重开</Button></Space>
        <div className="artifact-section"><Title level={4}>平台已采用修订</Title>{current.adoption ? <details><summary>采用关系与固定版本</summary><Output value={current.adoption} /></details> : <Paragraph type="secondary">尚未采用原生修订，确认将保留本次原始输出。</Paragraph>}</div>
        {nativeApplicable && <div className="artifact-section"><div className="section-head"><Title level={4}>原生当前修订</Title><Button onClick={() => void native.refetch()}>读取原生修订</Button></div>
          {native.isPending && <Text>正在核对原件与原生版本…</Text>}{native.isError && <Alert type="error" title={native.error.message} />}
          {native.data && <><Paragraph>原生版本 {native.data.current_revision} · 平台所见版本 {native.data.revision} · {native.data.native_review_qualification === 'independent' ? '原生复核资格独立' : '原生人工处置'}</Paragraph>
            {classification && typeof currentNative?.human_label_id === 'string' && <Paragraph><Text strong>当前人工类别：</Text>{currentNative.human_label_id}</Paragraph>}
            {!classification && <Paragraph style={{ whiteSpace: 'pre-wrap' }}><Text strong>当前校对内容：</Text>{typeof currentNative?.edited === 'string' ? currentNative.edited : typeof (currentNative?.edited as { text?: unknown } | undefined)?.text === 'string' ? (currentNative!.edited as { text: string }).text : typeof currentNative?.text === 'string' ? currentNative.text : '当前版本未提供可读文字'}</Paragraph>}
            <Button disabled={disabled || !adoption} onClick={() => submit([current.item_id], 'adopt', adoption)}>采用此项原生修订 {native.data.current_revision}</Button>
            {!adoption && <Paragraph type="secondary">当前修订不满足明确采用条件，请先在原生项目完成所需修订并重新读取。</Paragraph>}
            <details><summary>原生修订记录与来源身份</summary><Output value={native.data.current} /><Output value={native.data.history} /><Output value={native.data.source} /></details></>}
        </div>}
        <div><Title level={4}>此事项处置历史</Title>{history.isError && <Alert type="error" title={history.error.message} />}
          <ul>{history.data?.items.map(event => <li key={event.event_id}><Paragraph>{new Date(event.created_at).toLocaleString('zh-CN')} · {event.actor?.display_name || '历史身份未记录'} · {{ confirm: '确认', defer: '暂缓', reopen: '重开', adopt: '采用修订' }[event.action]} · 版本 {event.revision ?? '未记录'}</Paragraph>
            <Paragraph>{stateNames[String(event.previous_state.disposition)] || String(event.previous_state.disposition || '未记录')} → {stateNames[String(event.current_state.disposition)] || String(event.current_state.disposition || '未记录')}；{event.note || '未填写说明'}</Paragraph>
            {event.adoption && <details><summary>采用关系</summary><Output value={event.adoption} /></details>}</li>)}</ul>
          {history.data?.total === 0 && <Text type="secondary">尚无处置历史</Text>}
          {(history.data?.total || 0) > 30 && <Pagination current={historyPage} pageSize={30} total={history.data?.total} onChange={setHistoryPage} showSizeChanger={false} />}
        </div>
      </> : <Empty description={focused && direct.isPending ? '按原事项身份读取中…' : '选中的筛选已无待处理事项'} />}
    </div></div>
    {save.isError && <Alert type="error" title={save.error.message} />}
  </section>
}

function BatchDetail({ batchId, workspaceId }: { batchId: string; workspaceId: string }) {
  const client = useQueryClient()
  const [params] = useSearchParams()
  const [actor, setActor] = useState(localReviewer)
  const [dispositionBlocked, setDispositionBlocked] = useState(() => !!fixedRequest('disposition', batchId))
  const [materialRecoveryReady, setMaterialRecoveryReady] = useState(false)
  const [publication, setPublication] = useState(() => pendingPublication(batchId))
  const [publicationRefreshError, setPublicationRefreshError] = useState('')
  const batch = useQuery({ queryKey: ['batch', batchId], queryFn: ({ signal }) => request<Batch>(`/batches/${encodeURIComponent(batchId)}`, { signal }),
    refetchInterval: query => terminal.includes(query.state.data?.status || '') ? false : 2500 })
  const scenario = batch.data?.template.scenario
  const nativeProjects = useQuery({ queryKey: ['batch-native-projects', workspaceId, scenario], enabled: !!scenario && scenario !== 'data_compare',
    queryFn: ({ signal }) => request<{ items: { project_id: string; link_id: string | null }[] }>(`/workspaces/${encodeURIComponent(workspaceId)}/${scenario}/projects`, { signal }) })
  const control = useMutation({ mutationFn: (operation: string) => post<Batch>(`/batches/${encodeURIComponent(batchId)}/control`, { operation }),
    onSuccess: item => { client.setQueryData(['batch', batchId], item); void client.invalidateQueries({ queryKey: ['batches', workspaceId] }) } })
  function rememberPublication(value: ReviewPending | null) { setPublication(value); persistFixed('review', batchId, value) }
  const publish = useMutation({ mutationFn: (payload: ReviewArtifactRequest) => post<Batch>(`/batches/${encodeURIComponent(batchId)}/review-artifacts`, payload),
    onSuccess: async () => {
      rememberPublication(null)
      await Promise.all([client.invalidateQueries({ queryKey: ['batch', batchId] }), client.invalidateQueries({ queryKey: ['artifact-bundles', workspaceId] }), client.invalidateQueries({ queryKey: ['artifact-bundle', workspaceId] })])
    }, onError: (error, payload) => {
      const rejection = error instanceof HttpError && error.status === 409 && rejectedReviewRequests.includes(error.message) ? error.message : undefined
      rememberPublication({ payload, status: rejection ? 'conflict' : 'uncertain', ...(rejection ? { rejection } : {}) })
    } })
  function generate(kind: ReviewArtifactRequest['kind']) {
    if (!batch.data || !actor.display_name.trim() || publication || (dispositionBlocked || batch.data.summary.pending_operations) && (kind === 'final' || !materialRecoveryReady)) return
    const payload: ReviewArtifactRequest = { request_id: newRequestId(), expected_revision: batch.data.revision, actor: { ...actor, display_name: actor.display_name.trim() }, kind }
    rememberPublication({ payload, status: 'uncertain' }); publish.mutate(payload)
  }
  async function refreshPublication() {
    setPublicationRefreshError('')
    try {
      const latest = await batch.refetch()
      if (latest.error) throw latest.error
      if (latest.data?.summary.pending_operations) throw new Error('先核实原处置操作，再准备复核材料。')
      if (publication?.status === 'conflict' && rejectedReviewRequests.includes(publication.rejection || '')) { rememberPublication(null); publish.reset() }
    } catch (error) { setPublicationRefreshError((error as Error).message) }
  }
  const item = batch.data
  if (batch.isError) return <Alert type="error" title={batch.error.message} action={<Button onClick={() => void batch.refetch()}>重试</Button>} />
  if (!item) return <Text role="status">正在读取批次…</Text>
  if (item.workspace_id !== workspaceId) return <Alert type="warning" title="这个批次属于另一工作空间" description="请切换到对应工作空间后查看。" />
  const scope = encodeURIComponent(workspaceId)
  const projectId = nativeProjects.data?.items.find(project => project.link_id === item.template.config.link_id)?.project_id
  const nativeImport = item.receipts.native_import as { handoff_id?: string } | undefined
  const nativeInputs = item.receipts.native_inputs as { asset_set_id?: string } | undefined
  const nativeParams = new URLSearchParams({ workspace: workspaceId })
  if (projectId) nativeParams.set('project', projectId)
  if (nativeImport?.handoff_id) nativeParams.set('handoff', nativeImport.handoff_id)
  if (nativeInputs?.asset_set_id) nativeParams.set('set', nativeInputs.asset_set_id)
  nativeParams.set('return_to', nativeReviewReturn(params.get('return_to'), workspaceId) || `/batches?${params}`)
  const nativeUrl = `/apps/${item.template.scenario}/workflow?${nativeParams}`
  const dataSession = (item.receipts.session as { session_id?: string } | undefined)?.session_id
  const coverage = item.summary.coverage || {}
  const localDispositionPending = !!fixedRequest('disposition', batchId)
  const publicationDisabled = !actor.display_name.trim() || !!publication || (dispositionBlocked || !!item.summary.pending_operations) && !materialRecoveryReady || publish.isPending
  const finalReady = item.status === 'succeeded' && item.coverage.index_complete === true && item.summary.review_complete && item.summary.pending === 0 && item.summary.deferred === 0 && !item.summary.pending_operations
  return <><section className="panel"><div className="section-head"><div><Title level={2}>{item.template.name}</Title><Text type="secondary">{scenarioNames[item.template.scenario]} · 模板版本 {item.template_version} · {new Date(item.created_at).toLocaleString('zh-CN')}</Text></div>
      <Space wrap><Status value={item.status} /><Button onClick={() => void batch.refetch()}>刷新状态</Button>
        {item.capabilities.includes('cancel') && <Button loading={control.isPending} onClick={() => control.mutate('cancel')}>取消批次</Button>}
        {item.capabilities.includes('retry') && <Button loading={control.isPending} onClick={() => control.mutate('retry')}>核实并重试未完成部分</Button>}</Space></div>
    <div className="batch-summary" aria-label="批次结果摘要"><div><Text type="secondary">执行阶段</Text><strong>{stageLabel(item.stage)}</strong></div>
      <div><Text type="secondary">执行覆盖</Text><strong>成功 {item.summary.succeeded} / 共 {item.summary.total}</strong><span>失败 {item.summary.failed} · 未完成 {Math.max(0, item.summary.total - item.summary.succeeded - item.summary.failed)}</span></div>
      <div><Text type="secondary">本次复核</Text><strong>已确认 {item.summary.confirmed}</strong><span>待处理 {item.summary.pending} · 暂缓 {item.summary.deferred}</span></div>
      <div><Text type="secondary">成果</Text><strong>{stateNames[item.artifact_state] || item.artifact_state}</strong><span>{item.artifacts.length} 份固定版本成果</span></div></div>
    <Space wrap><Tag>{item.summary.execution_complete ? '执行已结束' : '执行尚未结束'}</Tag><Tag>{item.summary.review_complete ? '待处理记录已复核' : '复核尚未完成'}</Tag><Status value={item.artifact_state} /></Space>
    {item.template.scenario === 'data_compare' && Object.keys(coverage).length > 0 && <Paragraph className="batch-coverage" aria-label="表格比对覆盖">
      相同 {coverage.same_pairs || 0} · 差异 {coverage.different_pairs || 0} · 仅左 {coverage.only_left || 0} · 仅右 {coverage.only_right || 0} · 重复键左 {coverage.ambiguous_left_rows || 0} · 重复键右 {coverage.ambiguous_right_rows || 0}</Paragraph>}
    {!terminal.includes(item.status) && <Paragraph type="secondary">作业由本机服务在后台推进，刷新或关闭页面后可从处理批次继续查看。</Paragraph>}
    {item.status === 'cancelling' && <Alert type="info" title="已请求取消，等待原生任务真实终态" />}
    {item.artifact_state === 'collection_failed' && <Alert type="error" title="处理结果已保留，成果归集失败" description="使用可用的重试入口核实固定成果；不需要重新计算成功结果。" />}
    {control.isError && <Alert type="error" title={control.error.message} />}
    <div className="batch-start-fields"><label>本机复核人<Input aria-label="本机复核人" placeholder="填写本次编制人显示名" maxLength={100} value={actor.display_name} disabled={localDispositionPending || dispositionBlocked && !materialRecoveryReady || !!publication || publish.isPending}
      onChange={event => { const value = { ...actor, display_name: event.target.value }; setActor(value); localStorage.setItem('clear-local-reviewer', JSON.stringify(value)) }} /></label></div>
    <Paragraph type="secondary">显示名用于本机复核记录，非组织认证身份。原请求重放继续使用其固定编制人。</Paragraph>
  </section>
    <section className="panel"><div className="section-head"><Title level={3}>成果与下载</Title><Link to={`/artifacts?workspace=${scope}`}>成果中心</Link></div>
      <Paragraph>平台处置版本 {item.revision} · 全量索引 {item.coverage.index_complete ? '已完成' : '尚未完成'} · 已索引 {item.coverage.indexed_items || 0} / 应复核 {item.coverage.required_items || 0}</Paragraph>
      {item.delivery.status === 'outdated' && <Alert type="warning" title="复核成果已过期" description="平台判断或执行状态已改变；旧文件仍保留，请完成复核后生成新的材料。" />}
      {item.delivery.bundle_id && <Paragraph>最近复核交付：{item.delivery.kind === 'final' ? '正式复核成果' : '中间材料'} · 固定版本 {item.delivery.revision} · {item.delivery.status === 'current' ? '当前版本' : '已过期'} <Link to={`/artifacts?workspace=${scope}&bundle=${encodeURIComponent(item.delivery.bundle_id)}`}>查看最近复核包</Link></Paragraph>}
      <Space wrap><Button disabled={publicationDisabled} loading={publish.isPending} onClick={() => generate('intermediate')}>生成中间复核材料</Button>
        <Button type="primary" disabled={publicationDisabled || !finalReady || dispositionBlocked} loading={publish.isPending} onClick={() => generate('final')}>生成正式复核成果</Button></Space>
      {materialRecoveryReady && <Paragraph type="secondary">原操作已登记为待核实；可生成明确中间材料保留未完成条件，不能生成正式版或提交新的处置。</Paragraph>}
      {!finalReady && <Paragraph type="secondary">正式版需要执行成功、全量索引完成、全部结果与技术事项已确认、没有待核实操作。中间材料保留未完成条件。</Paragraph>}
      {publication && <Alert type="warning" title={publication.status === 'conflict' ? '复核材料所见版本冲突，原请求已保留' : '复核材料响应需要核实'} description={<><Paragraph>请求 {publication.payload.request_id} · 版本 {publication.payload.expected_revision} · {publication.payload.actor.display_name} · {publication.payload.kind === 'final' ? '正式版' : '中间材料'}</Paragraph>
        <Space wrap><Button disabled={publish.isPending} onClick={() => publish.mutate(publication.payload)}>核实原材料请求</Button><Button disabled={publish.isPending} onClick={() => void refreshPublication()}>{publication.status === 'conflict' ? '刷新核对并结束材料冲突请求' : '刷新核对，保留材料请求'}</Button></Space></>} />}
      {publish.isError && <Alert type="error" title={publish.error.message} />}{publicationRefreshError && <Alert type="error" title={publicationRefreshError} />}
      {item.artifacts.length ? item.artifacts.map(artifact => <div className="batch-artifact" key={artifact.bundle_id}><strong>{artifact.title}</strong><Tag>{artifact.kind === 'batch_review' ? artifact.bundle_id === item.delivery.bundle_id && item.delivery.status === 'current' ? '当前复核包' : '旧版复核包' : '原始固定成果'}</Tag><Space wrap>
        {artifact.files.map(file => file.availability === 'available' ? <a key={file.name} href={`${API}/workspaces/${scope}/artifacts/${encodeURIComponent(artifact.bundle_id)}/files/${encodeURIComponent(file.name)}`}>下载 {file.name}</a> : <Tag key={file.name} color="error">{file.name} 不可用</Tag>)}
        <Link to={`/artifacts?workspace=${scope}&bundle=${encodeURIComponent(artifact.bundle_id)}`}>查看成果与来源</Link></Space></div>) : <Text type="secondary">{item.artifact_state === 'collection_failed' ? '尚未成功归集，请查看具体问题。' : '成果会在对应阶段成功后登记，可按需下载。'}</Text>}
    </section>
    <IssueQueue key={item.batch_id} batch={item} nativeUrl={nativeUrl} actor={actor} writeBlocked={!!publication || publish.isPending} onBlocked={setDispositionBlocked} onMaterialReady={setMaterialRecoveryReady} />
    <details className="batch-setup"><summary>执行记录与高级工作台</summary><Output value={item.receipts} /><Space wrap>
      <Link to={`/apps/data?workspace=${scope}${dataSession ? `&session=${encodeURIComponent(dataSession)}` : ''}`}>数据工作台</Link>
      <Link to={item.template.scenario === 'classification' ? nativeUrl : `/apps/classification/workflow?workspace=${scope}`}>分类工作流程</Link><Link to={item.template.scenario === 'ocr' ? nativeUrl : `/apps/ocr/workflow?workspace=${scope}`}>OCR 工作流程</Link><Link to={`/jobs?workspace=${scope}`}>原生任务状态</Link></Space></details>
  </>
}

function NewBatch({ workspaceId, inputSetId, onCreated }: { workspaceId: string; inputSetId: string | null; onCreated: (batch: Batch) => void }) {
  const client = useQueryClient()
  const [templateKey, setTemplateKey] = useState<string>()
  const [left, setLeft] = useState<string>(), [right, setRight] = useState<string>()
  const [selected, setSelected] = useState<string[]>([])
  const [pendingRequest, setPendingRequest] = useState<RunRequest | null>(() => restoredRequest(workspaceId))
  const [preflightIssues, setPreflightIssues] = useState<Preflight['issues']>([])
  const [uploadBusy, setUploadBusy] = useState(false)
  const templates = useQuery({ queryKey: ['batch-templates', workspaceId], queryFn: ({ signal }) => request<{ items: Template[] }>(`/batch-templates?workspace_id=${encodeURIComponent(workspaceId)}`, { signal }) })
  const library = useQuery({ queryKey: ['batch-assets', workspaceId], queryFn: ({ signal }) => assets(workspaceId, signal) })
  const fixed = useQuery({ queryKey: ['batch-input-set', workspaceId, inputSetId], enabled: !!inputSetId,
    queryFn: ({ signal }) => request<{ members: Asset[] }>(`/workspaces/${encodeURIComponent(workspaceId)}/assets/sets/${encodeURIComponent(inputSetId || '')}`, { signal }) })
  const chosen = templates.data?.items.find(item => `${item.template_id}:${item.version}` === templateKey)
  const supported = (item: Asset) => chosen?.scenario === 'data_compare' ? /\.(csv|xlsx|parquet)$/i.test(item.original_name)
    : chosen?.scenario === 'classification' ? /\.(jpe?g|png|webp|bmp)$/i.test(item.original_name) : /\.(jpe?g|png|bmp|tiff?|webp|heic|heif)$/i.test(item.original_name)
  const choices = (fixed.data?.members || library.data)?.filter(supported).map(item => ({ value: item.asset_id, label: `${item.original_name} · 版本 ${item.revision}` })) || []
  useEffect(() => { if (fixed.data && chosen && chosen.scenario !== 'data_compare') setSelected(fixed.data.members.filter(supported).map(item => item.asset_id)) }, [fixed.data, chosen?.scenario])
  const run = useMutation({ mutationFn: async (payload: RunRequest) => {
    if (pendingRequest) {
      const previous = await request<{ items: Batch[] }>(`/batches?${new URLSearchParams({ workspace_id: workspaceId, request_key: payload.request_key })}`)
      if (previous.items.length) return previous.items[0]
      persistRequest(workspaceId, null)
      setPendingRequest(null)
    }
    const check = await post<Preflight>('/batches/preflight', payload)
    setPreflightIssues(check.issues || [])
    if (!check.ok) throw new Error('预检未通过，请处理下方具体问题后再启动。')
    persistRequest(workspaceId, payload)
    setPendingRequest(payload)
    try { return await post<Batch>('/batches', payload) }
    catch (failure) {
      const previous = await request<{ items: Batch[] }>(`/batches?${new URLSearchParams({ workspace_id: workspaceId, request_key: payload.request_key })}`)
      if (previous.items.length) return previous.items[0]
      throw failure
    }
  }, onSuccess: item => { persistRequest(workspaceId, null); setPendingRequest(null); onCreated(item); void client.invalidateQueries({ queryKey: ['batches', workspaceId] }) } })
  const begin = () => {
    if (pendingRequest) { run.mutate(pendingRequest); return }
    if (!chosen || !library.data) return
    const roles = chosen.scenario === 'data_compare' ? [{ role: 'left', id: left }, { role: 'right', id: right }] : selected.map(id => ({ role: chosen.scenario === 'classification' ? 'images' : 'documents', id }))
    const inputs = roles.map(role => { const asset = library.data!.find(item => item.asset_id === role.id)!; return { role: role.role, asset_id: asset.asset_id, asset_revision: asset.revision, blob_hash: asset.blob_hash } })
    run.mutate({ schema_version: '1.0', request_key: 'r' + crypto.randomUUID(), workspace_id: workspaceId, template_id: chosen.template_id, template_version: chosen.version, inputs })
  }
  const valid = chosen && (chosen.scenario === 'data_compare' ? left && right && left !== right : selected.length > 0)
  const saved = (item: Template) => {
    client.setQueryData<{ items: Template[] }>(['batch-templates', workspaceId], old => ({ items: [...(old?.items || []).filter(existing => existing.template_id !== item.template_id || existing.version !== item.version), item] }))
    setTemplateKey(`${item.template_id}:${item.version}`)
  }
  return <><section className="panel"><div className="section-head"><Title level={3}>新建处理批次</Title><Link to={`/assets?workspace=${encodeURIComponent(workspaceId)}`}>管理资料</Link></div>
    <Paragraph type="secondary">选资料、选已确认的模板、启动一次。成功阶段会连续执行并归集成果，需要你决定的问题会单独列出。</Paragraph>
    <div className="batch-start-fields"><label>已确认的处理模板<Select aria-label="已确认的处理模板" placeholder="选择处理模板" value={templateKey} loading={templates.isPending} disabled={!!pendingRequest || run.isPending}
      onChange={value => { setTemplateKey(value); setLeft(undefined); setRight(undefined); setSelected([]); setPreflightIssues([]); run.reset() }}
      options={templates.data?.items.map(item => ({ value: `${item.template_id}:${item.version}`, label: `${item.name} · ${scenarioNames[item.scenario]} · v${item.version}` }))} /></label>
      {chosen?.scenario === 'data_compare' ? <><label>左表资料<Select aria-label="批次左表资料" placeholder="选择左表" value={left} onChange={setLeft} options={choices} showSearch optionFilterProp="label" disabled={!!pendingRequest || run.isPending} /></label>
        <label>右表资料<Select aria-label="批次右表资料" placeholder="选择右表" value={right} onChange={setRight} options={choices} showSearch optionFilterProp="label" disabled={!!pendingRequest || run.isPending} /></label></> : chosen && <label>输入资料<Select aria-label="批次输入资料" mode="multiple" placeholder="选择本批次资料" value={selected} onChange={setSelected} options={choices} optionFilterProp="label" disabled={!!pendingRequest || run.isPending} /></label>}
    </div>
    {chosen && <TemplateSummary template={chosen} />}
    <UploadQueue workspaceId={workspaceId} disabled={!!pendingRequest || run.isPending} onBusy={setUploadBusy} />
    <div className="batch-start-actions"><Button type="primary" disabled={!pendingRequest && !valid || uploadBusy} loading={run.isPending} onClick={begin}>{pendingRequest ? '核实原请求并继续' : '预检并启动一次'}</Button></div>
    {chosen?.scenario === 'ocr' && <Paragraph type="secondary">首版平台自动识别接收图像。PDF 与仅校对模式请使用原生工作台，已有结果可以校对和归集。</Paragraph>}
    {templates.data?.items.length === 0 && <Alert type="info" title="尚无已确认模板" description="首次在下方配置并保存，重复批次复用同一版本。" />}
    {[templates.error, library.error, fixed.error, run.error].filter(Boolean).map((error, index) => <Alert key={index} type="error" title={(error as Error).message} />)}
    {pendingRequest && <Alert type="warning" title="原请求状态需要核实" description="继续时先查询原运行身份，再复用原请求；输入暂时锁定以避免重复提交。" />}
    {preflightIssues.map((issue, index) => <Alert key={index} type="warning" title={issue.title || issue.code || '预检问题'} description={issue.detail || issue.message} />)}
  </section><TemplateSetup workspaceId={workspaceId} onSaved={saved} /></>
}

export function BatchCenter() {
  const [params, setParams] = useSearchParams()
  const workspaceId = params.get('workspace'), batchId = params.get('batch'), inputSetId = params.get('set')
  const list = useQuery({ queryKey: ['batches', workspaceId], enabled: !!workspaceId && !batchId,
    queryFn: ({ signal }) => request<{ items: Batch[] }>(`/batches?workspace_id=${encodeURIComponent(workspaceId || '')}`, { signal }), refetchInterval: 5000 })
  const open = (id: string | null) => { const next = new URLSearchParams(params); if (id) next.set('batch', id); else next.delete('batch'); setParams(next) }
  return <div className="batch-center"><div className="page-heading"><Text className="eyebrow">CLEAR / BATCHES</Text><Title level={1}>处理批次</Title>
    <Paragraph>后台自动执行，先看结果、待处理项和成果。</Paragraph></div>
    {!workspaceId ? <Empty description="先在顶部选择工作空间，或新建一个工作空间" ><Link to="/workspaces">管理工作空间</Link></Empty> : batchId ? <>
      <Button className="batch-back" onClick={() => open(null)}>← 返回批次列表与新建入口</Button><BatchDetail key={batchId} batchId={batchId} workspaceId={workspaceId} />
    </> : <><NewBatch key={workspaceId} workspaceId={workspaceId} inputSetId={inputSetId} onCreated={batch => open(batch.batch_id)} />
      <section className="panel"><div className="section-head"><Title level={3}>已有批次</Title><Button onClick={() => void list.refetch()}>刷新</Button></div>
        {list.isError && <Alert type="error" title={list.error.message} />}
        <Table<Batch> size="small" rowKey="batch_id" dataSource={list.data?.items || []} loading={list.isPending} pagination={{ pageSize: 10, showSizeChanger: false }} scroll={{ x: 760 }}
          columns={[{ title: '批次', key: 'name', render: (_, item) => <Button type="link" onClick={() => open(item.batch_id)}>{item.template.name}</Button> },
            { title: '执行', dataIndex: 'status', render: value => <Status value={value} /> }, { title: '结果', key: 'summary', render: (_, item) => `成功 ${item.summary.succeeded} · 失败 ${item.summary.failed}` },
            { title: '待处理', key: 'pending', render: (_, item) => `${item.summary.pending} · 暂缓 ${item.summary.deferred}` }, { title: '成果', dataIndex: 'artifact_state', render: value => <Status value={value} /> },
            { title: '时间', dataIndex: 'created_at', render: value => new Date(value).toLocaleString('zh-CN') }]}
          locale={{ emptyText: <Empty description="尚无批次，选择模板和资料启动第一次处理" /> }} />
      </section></>}
  </div>
}
