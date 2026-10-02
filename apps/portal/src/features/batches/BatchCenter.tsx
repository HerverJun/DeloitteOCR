import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Pagination, Select, Space, Table, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { API, assets, pendingRequest as restoredRequest, persistRequest, post, request, scenarioNames, stageLabel, stateNames, terminal, type Asset, type Batch, type Issue, type Preflight, type RunRequest, type Template } from './api'
import { TemplateSetup } from './TemplateSetup'
import './batches.css'

const { Title, Text, Paragraph } = Typography
const valueNames: Record<string, string> = { category: '结果分类', label: '模型建议', predicted_label: '模型建议', predicted_label_id: '模型建议', confidence: '分数', score: '分数', name: '名称', text: '识别文本', message: '信息', error: '错误', left: '左表', right: '右表', field: '字段', reason: '原因', page_number: '页码', source_row: '原行', left_source_row: '左表原行', right_source_row: '右表原行' }
function print(value: unknown): string { return typeof value === 'string' ? value : value === null ? '空值' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value) }
function Output({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <Paragraph type="secondary">此事项没有附加输出</Paragraph>
  if (typeof value !== 'object') return <Paragraph>{print(value)}</Paragraph>
  return <dl className="batch-output">{Object.entries(value).map(([key, item]) => <div key={key}><dt>{valueNames[key] || key}</dt><dd>{print(item)}</dd></div>)}</dl>
}
function Status({ value }: { value: string }) { return <Tag color={value === 'succeeded' || value === 'collected' ? 'success' : value.includes('failed') ? 'error' : value === 'running' || value === 'queued' ? 'processing' : 'default'}>{stateNames[value] || value}</Tag> }

function IssueQueue({ batch, nativeUrl }: { batch: Batch; nativeUrl: string }) {
  const client = useQueryClient()
  const [filter, setFilter] = useState('pending')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<string[]>([])
  const [focused, setFocused] = useState<string>()
  const [note, setNote] = useState('')
  const classification = batch.template.scenario === 'classification'
  useEffect(() => { setSelected([]); setFocused(undefined); setNote('') }, [filter, page, batch.batch_id])
  const issues = useQuery({ queryKey: ['batch-issues', batch.batch_id, filter, page],
    queryFn: ({ signal }) => request<{ items: Issue[]; total: number }>(`/batches/${encodeURIComponent(batch.batch_id)}/issues?offset=${(page - 1) * 20}&limit=20${filter === 'all' ? '' : `&disposition=${filter}`}`, { signal }),
    refetchInterval: terminal.includes(batch.status) ? false : 3000 })
  const current = issues.data?.items.find(item => item.item_id === focused) || issues.data?.items[0]
  const save = useMutation({ mutationFn: ({ itemIds, action }: { itemIds: string[]; action: 'confirm' | 'defer' | 'reopen' }) => post(`/batches/${encodeURIComponent(batch.batch_id)}/dispositions`, { item_ids: itemIds, action, note }),
    onSuccess: async () => {
      const records = issues.data?.items || []
      const index = records.findIndex(item => item.item_id === current?.item_id)
      setFocused(records[index + 1]?.item_id); setNote(''); setSelected([])
      await client.invalidateQueries({ queryKey: ['batch-issues', batch.batch_id] })
      void client.invalidateQueries({ queryKey: ['batch', batch.batch_id] })
      if (page > 1 && records.length === 1 && filter !== 'all') setPage(page - 1)
    } })
  const source = current?.source || {}
  const sourceAsset = typeof source.asset_id === 'string' ? source.asset_id : null
  const sourceRevision = source.asset_revision || source.revision
  const sourceUrl = sourceAsset ? `${API}/workspaces/${encodeURIComponent(batch.workspace_id)}/assets/${encodeURIComponent(sourceAsset)}/content${sourceRevision ? `?revision=${sourceRevision}` : ''}` : null
  const sourceName = String(source.name || source.original_name || '')
  const image = /\.(jpe?g|png|webp|bmp|tiff?)$/i.test(sourceName)
  return <section className="panel"><div className="section-head"><Title level={3}>异常与复核</Title>
    <Select aria-label="复核状态筛选" value={filter} onChange={value => { setFilter(value); setPage(1) }} options={[{ value: 'pending', label: '待处理' }, { value: 'deferred', label: '暂缓' }, { value: 'confirmed', label: '已确认' }, { value: 'all', label: '全部记录' }]} /></div>
    <Paragraph type="secondary">原始输出与本次处置分别保留。暂缓仍属于待处理事项，技术执行成功不代表复核完成。</Paragraph>
    {classification && <Paragraph type="secondary">确认表示接受本次原始模型建议。需要改标签时进入 <Link to={nativeUrl}>原生项目复核</Link>。</Paragraph>}
    {batch.template.scenario === 'ocr' && <Paragraph type="secondary">识别文本需要采用或校对时进入 <Link to={nativeUrl}>当前原生 OCR 项目</Link>，再返回确认本次固定结果。</Paragraph>}
    {issues.isError && <Alert type="error" title={issues.error.message} action={<Button onClick={() => void issues.refetch()}>重试</Button>} />}
    <div className="batch-review-layout"><div className="batch-queue">
      <div className="batch-selection"><Text>本页已明确选中 {selected.length} 项</Text><Button size="small" disabled={!selected.length} onClick={() => setSelected([])}>清除选中</Button></div>
      <Table<Issue> size="small" rowKey="item_id" dataSource={issues.data?.items || []} loading={issues.isPending} pagination={false}
        rowSelection={{ selectedRowKeys: selected, onChange: keys => setSelected(keys.map(String)) }}
        rowClassName={item => item.item_id === current?.item_id ? 'batch-active-issue' : ''}
        columns={[{ title: '事项', key: 'title', render: (_, item) => <Button type="link" className="batch-issue-link" onClick={() => { setFocused(item.item_id); setNote('') }}>{item.title}</Button> },
          { title: '处置', dataIndex: 'disposition', width: 100, render: value => <Status value={value} /> }]}
        locale={{ emptyText: <Empty description={issues.isPending ? '读取复核记录…' : '当前筛选没有事项'} /> }} />
      <div className="batch-pagination"><Pagination current={page} pageSize={20} total={issues.data?.total || 0} onChange={setPage} showSizeChanger={false} size="small" />
        <Text type="secondary">全选只选择当前页，切页或切换筛选会清除选中。</Text></div>
      <Space wrap><Button disabled={!selected.length || save.isPending} onClick={() => save.mutate({ itemIds: selected, action: 'confirm' })}>{classification ? '确认选中建议' : '确认选中'} {selected.length} 项</Button>
        <Button disabled={!selected.length || save.isPending} onClick={() => save.mutate({ itemIds: selected, action: 'defer' })}>暂缓选中</Button>
        <Button disabled={!selected.length || save.isPending} onClick={() => save.mutate({ itemIds: selected, action: 'reopen' })}>重开选中</Button></Space>
    </div><div className="batch-review-item">
      {current ? <><div className="section-head"><Title level={4}>{current.title}</Title><Status value={current.disposition} /></div>
        <Paragraph>{current.detail}</Paragraph>
        <div className="batch-evidence"><div><Text strong>原件与来源</Text>
          {sourceUrl && (image ? <a href={sourceUrl} target="_blank" rel="noreferrer"><img src={sourceUrl} alt={sourceName || '当前事项原件'} /></a> : <p><a href={sourceUrl} target="_blank" rel="noreferrer">打开原件 {sourceName}</a></p>)}
          <details><summary>来源版本与定位</summary><Output value={source} /></details></div><div><Text strong>固定版本的原始输出</Text><Output value={current.raw_output} /></div></div>
        {current.note && <Paragraph type="secondary">上次说明：{current.note}</Paragraph>}
        <Input.TextArea aria-label="复核说明" placeholder="处置说明（可选）" rows={2} maxLength={2000} value={note} onChange={event => setNote(event.target.value)} />
        <Space wrap className="batch-review-actions"><Button type="primary" loading={save.isPending} onClick={() => save.mutate({ itemIds: [current.item_id], action: 'confirm' })}>{classification ? '确认模型建议并下一项' : '确认并下一项'}</Button>
          <Button disabled={save.isPending} onClick={() => save.mutate({ itemIds: [current.item_id], action: 'defer' })}>暂缓并下一项</Button>
          <Button disabled={save.isPending || current.disposition === 'pending'} onClick={() => save.mutate({ itemIds: [current.item_id], action: 'reopen' })}>重开</Button></Space>
      </> : <Empty description="选中的筛选已无待处理事项" />}
    </div></div>
    {save.isError && <Alert type="error" title={save.error.message} />}
  </section>
}

function BatchDetail({ batchId, workspaceId }: { batchId: string; workspaceId: string }) {
  const client = useQueryClient()
  const batch = useQuery({ queryKey: ['batch', batchId], queryFn: ({ signal }) => request<Batch>(`/batches/${encodeURIComponent(batchId)}`, { signal }),
    refetchInterval: query => terminal.includes(query.state.data?.status || '') ? false : 2500 })
  const scenario = batch.data?.template.scenario
  const nativeProjects = useQuery({ queryKey: ['batch-native-projects', workspaceId, scenario], enabled: !!scenario && scenario !== 'data_compare',
    queryFn: ({ signal }) => request<{ items: { project_id: string; link_id: string | null }[] }>(`/workspaces/${encodeURIComponent(workspaceId)}/${scenario}/projects`, { signal }) })
  const control = useMutation({ mutationFn: (operation: string) => post<Batch>(`/batches/${encodeURIComponent(batchId)}/control`, { operation }),
    onSuccess: item => { client.setQueryData(['batch', batchId], item); void client.invalidateQueries({ queryKey: ['batches', workspaceId] }) } })
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
  const nativeUrl = `/apps/${item.template.scenario}/workflow?${nativeParams}`
  const dataSession = (item.receipts.session as { session_id?: string } | undefined)?.session_id
  const coverage = item.summary.coverage || {}
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
  </section>
    <section className="panel"><div className="section-head"><Title level={3}>成果与下载</Title><Link to={`/artifacts?workspace=${scope}`}>成果中心</Link></div>
      {item.artifacts.length ? item.artifacts.map(artifact => <div className="batch-artifact" key={artifact.bundle_id}><strong>{artifact.title}</strong><Space wrap>
        {artifact.files.map(file => file.availability === 'available' ? <a key={file.name} href={`${API}/workspaces/${scope}/artifacts/${encodeURIComponent(artifact.bundle_id)}/files/${encodeURIComponent(file.name)}`}>下载 {file.name}</a> : <Tag key={file.name} color="error">{file.name} 不可用</Tag>)}
        <Link to={`/artifacts?workspace=${scope}&bundle=${encodeURIComponent(artifact.bundle_id)}`}>查看成果与来源</Link></Space></div>) : <Text type="secondary">{item.artifact_state === 'collection_failed' ? '尚未成功归集，请查看具体问题。' : '成果会在对应阶段成功后登记，可按需下载。'}</Text>}
    </section>
    <IssueQueue key={item.batch_id} batch={item} nativeUrl={nativeUrl} />
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
  const templates = useQuery({ queryKey: ['batch-templates', workspaceId], queryFn: ({ signal }) => request<{ items: Template[] }>(`/batch-templates?workspace_id=${encodeURIComponent(workspaceId)}`, { signal }) })
  const library = useQuery({ queryKey: ['batch-assets', workspaceId], queryFn: ({ signal }) => assets(workspaceId, signal) })
  const fixed = useQuery({ queryKey: ['batch-input-set', workspaceId, inputSetId], enabled: !!inputSetId,
    queryFn: ({ signal }) => request<{ members: Asset[] }>(`/workspaces/${encodeURIComponent(workspaceId)}/assets/sets/${encodeURIComponent(inputSetId || '')}`, { signal }) })
  const chosen = templates.data?.items.find(item => `${item.template_id}:${item.version}` === templateKey)
  const supported = (item: Asset) => chosen?.scenario === 'data_compare' ? /\.(csv|xlsx|parquet)$/i.test(item.original_name)
    : chosen?.scenario === 'classification' ? /\.(jpe?g|png|webp|bmp)$/i.test(item.original_name) : /\.(jpe?g|png|bmp|tiff?|webp|heic|heif)$/i.test(item.original_name)
  const choices = (fixed.data?.members || library.data)?.filter(supported).map(item => ({ value: item.asset_id, label: `${item.original_name} · 版本 ${item.revision}` })) || []
  useEffect(() => { if (fixed.data && chosen && chosen.scenario !== 'data_compare') setSelected(fixed.data.members.filter(supported).map(item => item.asset_id)) }, [fixed.data, chosen?.scenario])
  const upload = useMutation({ mutationFn: async (files: File[]) => {
    for (const file of files) await request(`/workspaces/${encodeURIComponent(workspaceId)}/assets/uploads?name=${encodeURIComponent(file.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file })
  }, onSettled: () => { void client.invalidateQueries({ queryKey: ['batch-assets', workspaceId] }); void client.invalidateQueries({ queryKey: ['assets', workspaceId] }) } })
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
    <div className="batch-start-actions"><label className="batch-upload">{upload.isPending ? '上传资料中…' : '添加新资料'}<input aria-label="添加批次资料" type="file" multiple disabled={upload.isPending || !!pendingRequest || run.isPending}
      onChange={event => { const files = Array.from(event.target.files || []); event.target.value = ''; if (files.length) upload.mutate(files) }} /></label>
      <Button type="primary" disabled={!pendingRequest && !valid || upload.isPending} loading={run.isPending} onClick={begin}>{pendingRequest ? '核实原请求并继续' : '预检并启动一次'}</Button></div>
    {chosen?.scenario === 'ocr' && <Paragraph type="secondary">首版平台自动识别接收图像。PDF 与仅校对模式请使用原生工作台，已有结果可以校对和归集。</Paragraph>}
    {templates.data?.items.length === 0 && <Alert type="info" title="尚无已确认模板" description="首次在下方配置并保存，重复批次复用同一版本。" />}
    {[templates.error, library.error, fixed.error, upload.error, run.error].filter(Boolean).map((error, index) => <Alert key={index} type="error" title={(error as Error).message} />)}
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
