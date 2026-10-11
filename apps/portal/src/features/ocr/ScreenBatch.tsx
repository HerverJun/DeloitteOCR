import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Checkbox, Empty, Input, Modal, Progress, Select, Space, Table, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { ScreenDiagnostics } from './ScreenDiagnostics'
import { LocalCandidates } from './LocalCandidates'
import { BusinessMetadata, metadataEdits, type MetadataItem, type MetadataDraft } from './BusinessMetadata'
import { screenSubmissionPolicy } from './screenSubmissionPolicy'
import { reliableDiagnosticBox, diagnosticRange, currentResultRevision, type Diagnostic, type Diagnostics, type CandidateComparison } from './screenDiagnosticWorkflow'

type Item = { input_version_id: string; image_id: string; asset_id: string; name: string; status: string; stage: string; review_state?: string; result_id?: string; result_revision?: number; current_result_revision?: number; review_stale?: boolean; human_confirmed?: boolean; issues: { code?: string; message?: string; reason?: string }[] }
type StageEta = { remaining_seconds: number | null; stage?: { label?: string } | null; sample_count: number; scope: string; reason?: string; not_included?: string[] }
type Batch = { batch_id: string; status: string; counts: { total: number; completed: number; needs_review: number; failed: number; cancelled: number; pending: number }; items: Item[]; total?: number; filtered_total?: number; capabilities: string[]; eta?: StageEta; delivery_snapshot_id?: string; delivery_fresh?: boolean; stale_item_count?: number }
type Cell = { row?: number; column?: number; row_index?: number; col_index?: number; text?: string; content?: string }
type BusinessTable = { cells?: Cell[]; columns?: number; caption?: string }
type Detail = Item & { review_binding_token?: string; business_metadata?: MetadataItem[]; business_metadata_source?: string; diagnostics?: Diagnostics; original_url: string; result?: { revision?: number; tables?: BusinessTable[] }; tables?: BusinessTable[]; candidates?: { result_id?: string; engine?: string; tables?: BusinessTable[] }[]; preparation?: { width?: number; height?: number; prepared_images?: { id: string; kind: string; url: string }[] } }
const detailBinding = (value?: Detail) => value ? `${value.input_version_id}:${value.result_id}:${currentResultRevision(value)}:${value.review_binding_token || ''}` : undefined
type Bundle = { bundle_id: string; files: { name: string }[] }
type NumericColumns = Record<string, Record<string, number[]>>
class ScreenError extends Error { constructor(message: string, readonly status: number) { super(message) } }
const labels: Record<string, string> = { completed: '已完成', needs_review: '待复核', failed: '失败', cancelled: '已取消', pending: '待处理', running: '处理中', paused: '已暂停' }
async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) { const data = await response.json().catch(() => ({})); throw new ScreenError(typeof data.detail === 'string' ? data.detail : `操作未完成（${response.status}）`, response.status) }
  return response.json() as Promise<T>
}
const post = (data: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data) })
const requestId = () => 'r' + crypto.randomUUID().replaceAll('-', '')
async function identified<T>(key: string, url: string, data: Record<string, unknown>) {
  const fingerprint = JSON.stringify(data)
  const old = JSON.parse(sessionStorage.getItem(key) || 'null') as { fingerprint: string; request_id: string } | null
  if (old && old.fingerprint !== fingerprint) throw new Error('原操作回包待核实，请保持原内容重试')
  const attempt = old || { fingerprint, request_id: requestId() }; sessionStorage.setItem(key, JSON.stringify(attempt))
  try { const result = await json<T>(url, post({ ...data, request_id: attempt.request_id })); sessionStorage.removeItem(key); return result }
  catch (error) { if (error instanceof ScreenError && error.status < 500) sessionStorage.removeItem(key); throw error }
}

export function ScreenBatch({ workspaceId, linkId, setId, revision, count, ready }: { workspaceId: string; linkId?: string | null; setId: string; revision: number; count: number; ready: boolean }) {
  const [params, setParams] = useSearchParams(), client = useQueryClient(), batchId = params.get('screen_batch')
  const [mode, setMode] = useState<'auto' | 'accuracy'>('auto'), [status, setStatus] = useState<string>(), [page, setPage] = useState(1)
  const [includeReview, setIncludeReview] = useState(false), [review, setReview] = useState<Item | null>(null), [edits, setEdits] = useState<Record<string, string>>({})
  const [crop, setCrop] = useState<string>()
  const [metadataDrafts, setMetadataDrafts] = useState<Record<number, MetadataDraft>>({}), [draftBinding, setDraftBinding] = useState<string>()
  const [candidate, setCandidate] = useState<string>()
  const [diagnostic, setDiagnostic] = useState<Diagnostic>()
  const [diagnosticNotice, setDiagnosticNotice] = useState('')
  const [reviewNotice, setReviewNotice] = useState('')
  const [selected, setSelected] = useState<string[]>([])
  const [numericByBatch, setNumericByBatch] = useState<Record<string, NumericColumns>>({})
  const numericColumns = batchId ? numericByBatch[batchId] || {} : {}
  const base = `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/ocr/screen-batches`
  const batch = useQuery({ queryKey: ['ocr-screen-batch', workspaceId, batchId, page, status], enabled: !!batchId,
    queryFn: ({ signal }) => json<Batch>(`${base}/${batchId}?offset=${(page - 1) * 30}&limit=30${status ? `&status=${status}` : ''}`, { signal }),
    refetchInterval: q => q.state.data?.counts.pending ? 2500 : false })
  const refresh = () => void client.invalidateQueries({ queryKey: ['ocr-screen-batch', workspaceId] })
  const start = useMutation({ mutationFn: () => identified<Batch>(`clear-screen-submit:${workspaceId}:${setId}:${revision}`, base,
    { link_id: linkId, asset_set_id: setId, asset_set_revision: revision, policy: screenSubmissionPolicy(count, mode) }),
    onSuccess: value => { const next = new URLSearchParams(params); next.set('screen_batch', value.batch_id); setParams(next); refresh() } })
  const control = useMutation({ mutationFn: (command: string | { action: string; version_ids: string[] }) => {
    const action = typeof command === 'string' ? command : command.action
    return identified<Batch>(`clear-screen-action:${workspaceId}:${batchId}`, `${base}/${batchId}/action`, { action, ...(action === 'upgrade' ? { version_ids: typeof command === 'string' ? selected : command.version_ids } : {}) })
  }, onSuccess: value => {
    if (value.batch_id !== batchId) { const next = new URLSearchParams(params); next.set('screen_batch', value.batch_id); setParams(next); setSelected([]); setPage(1); setStatus(undefined) }
    refresh()
  } })
  const publish = useMutation({ mutationFn: () => json<Bundle>(`${base}/${batchId}/export`, post({ include_pending_review: includeReview, ...(Object.keys(numericColumns).length ? { numeric_columns: numericColumns } : {}) })) })
  const exportFreshness = useQuery({ queryKey: ['ocr-screen-export-freshness', workspaceId, publish.data?.bundle_id, batch.data?.delivery_snapshot_id], enabled: !!publish.data?.bundle_id,
    queryFn: ({ signal }) => json<{ current: boolean; state: string }>(`/api/platform/v1/workspaces/${workspaceId}/artifacts/${publish.data!.bundle_id}/freshness`, { signal }) })
  const detail = useQuery({ queryKey: ['ocr-screen-detail', workspaceId, batchId, review?.input_version_id], enabled: !!review,
    queryFn: ({ signal }) => json<Detail>(`${base}/${batchId}/items/${review!.input_version_id}`, { signal }) })
  useEffect(() => {
    if (review && detail.data?.input_version_id === review.input_version_id &&
      (!draftBinding || (!Object.keys(edits).length && !Object.keys(metadataDrafts).length && !candidate))) {
      setDraftBinding(detailBinding(detail.data))
    }
  }, [review, detail.data, draftBinding, edits, metadataDrafts, candidate])
  const staleDraft = !!draftBinding && draftBinding !== detailBinding(detail.data)
  const comparison = useMutation({ mutationFn: () => {
    const currentRevision = currentResultRevision(detail.data)
    if (!candidate || !detail.data?.result_id || currentRevision === undefined || !detail.data.candidates?.some(value => value.result_id === candidate)) throw new Error('请先选择一个当前图片的候选结果')
    return json<CandidateComparison>(`${base}/${batchId}/items/${detail.data.input_version_id}/compare`, post({ result_id: detail.data.result_id, expected_revision: currentRevision, candidate_id: candidate }))
  }, onSuccess: () => setDiagnostic(undefined) })
  const validComparison = comparison.data && comparison.data.result_revision === currentResultRevision(detail.data) && comparison.data.input_version_id === detail.data?.input_version_id && comparison.data.result_id === detail.data?.result_id ? comparison.data : undefined
  const diagnosticData = validComparison?.diagnostics || detail.data?.diagnostics
  const currentDiagnostic = diagnosticData?.items.find(value => value.id === diagnostic?.id) || diagnosticData?.items[0]
  const reliableBox = reliableDiagnosticBox(currentDiagnostic)
  const tables = (candidate ? detail.data?.candidates?.find(value => value.result_id === candidate)?.tables : detail.data?.tables || detail.data?.result?.tables) || []
  const cells = tables.flatMap((table, tableIndex) => (table.cells || []).map(cell => ({
    key: `${tableIndex}:${cell.row ?? cell.row_index ?? 0}:${cell.column ?? cell.col_index ?? 0}`, table_index: tableIndex,
    row: cell.row ?? cell.row_index ?? 0, column: cell.column ?? cell.col_index ?? 0, before: cell.text ?? cell.content ?? '' })))
  const hasDraft = !!candidate || Object.keys(metadataDrafts).length > 0 || cells.some(cell => edits[cell.key] !== undefined && edits[cell.key] !== cell.before)
  const closeReview = () => {
    if (save.isPending) return
    if (hasDraft) { setReviewNotice('请先保存或清除当前表格和业务说明修改，再关闭复核窗口'); return }
    setReview(null); setReviewNotice('')
  }
  const save = useMutation({ mutationFn: async (state: string) => {
    const selected = detail.data
    const currentRevision = currentResultRevision(selected)
    if (!selected?.result_id || currentRevision === undefined) throw new Error('尚无可核实的最终结果')
    if (staleDraft) throw new Error('复核期间结果或人工确认已变化，请重新加载后核对')
    return identified(`clear-screen-review:${workspaceId}:${batchId}:${selected.input_version_id}`, `${base}/${batchId}/action`, {
      action: 'review', version_ids: [selected.input_version_id], result_id: selected.result_id, expected_revision: currentRevision, state,
      ...(selected.review_binding_token ? { expected_review_token: selected.review_binding_token } : {}),
      ...(candidate ? { candidate_id: candidate } : {}),
      ...(Object.keys(metadataDrafts).length ? { metadata_edits: metadataEdits(selected.business_metadata || [], metadataDrafts) } : {}),
      edits: cells.filter(cell => edits[cell.key] !== undefined && edits[cell.key] !== cell.before).map(({ key, ...cell }) => ({ ...cell, after: edits[key] })) })
  }, onSuccess: () => {
    if (batchId && review) setNumericByBatch(old => {
      const remaining = { ...(old[batchId] || {}) }; delete remaining[review.input_version_id]
      return { ...old, [batchId]: remaining }
    })
    setReview(null); setEdits({}); setMetadataDrafts({}); setDraftBinding(undefined); setReviewNotice(''); refresh(); void client.invalidateQueries({ queryKey: ['ocr-screen-detail', workspaceId, batchId] })
  } })
  const counts = batch.data?.counts
  const eta = batch.data?.eta
  const estimated = batch.data?.status === 'running' && eta?.scope === 'current_queued_stage' && eta.reason === 'estimated' && eta.sample_count >= 3 && typeof eta.remaining_seconds === 'number' && Number.isFinite(eta.remaining_seconds) && eta.remaining_seconds >= 0
  const remaining = estimated ? (eta!.remaining_seconds! < 60 ? `${Math.ceil(eta!.remaining_seconds!)} 秒` : `${Math.ceil(eta!.remaining_seconds! / 60)} 分钟`) : undefined
  return <section className="panel ocr-screen-batch"><div className="section-head"><Typography.Title level={3}>批量图片转 Excel</Typography.Title><Tag>每张图片一个文件</Tag></div>
    {!batchId ? <><Typography.Paragraph type="secondary">一次处理最多 1000 张图片，先定位业务表区，再识别和集中复核疑点，最后下载 ZIP。</Typography.Paragraph>
      <Space wrap><Typography.Text>{count} 张</Typography.Text><Select aria-label="批次处理策略" value={mode} onChange={setMode} options={[{ value: 'auto', label: '自动处理' }, { value: 'accuracy', label: '提高精度（耗时增加，仍需核对）' }]} />
        <Button type="primary" disabled={!ready || !linkId || !count || count > 1000} loading={start.isPending} onClick={() => start.mutate()}>开始整批转 Excel</Button></Space></> : <>
      {counts && <><Space wrap>{(['total', 'completed', 'pending', 'needs_review', 'failed', 'cancelled'] as const).map(key => <Typography.Text key={key}>{({ total: '总数', completed: '完成', pending: '处理中', needs_review: '待复核', failed: '失败', cancelled: '取消' })[key]} {counts[key]}</Typography.Text>)}<Tag>{labels[batch.data!.status] || batch.data!.status}</Tag></Space>
        <Progress percent={Math.round((counts.total - counts.pending) * 100 / Math.max(counts.total, 1))} status={counts.pending ? 'active' : counts.failed ? 'exception' : 'normal'} />
        <Typography.Paragraph type="secondary">已处理包含待复核、失败和取消项。可先下载当前可导出范围，剩余 {counts.pending} 张继续处理。</Typography.Paragraph>
        {batch.data?.delivery_fresh === false && <Alert type="warning" title={`已有导出是历史版本，${batch.data.stale_item_count || 0} 张图片版本已变化`} description="历史下载保留；确认当前范围后重新导出。" />}
        {!!counts.pending && eta && <Typography.Paragraph type="secondary">{estimated ? `当前阶段预计还需约 ${remaining}${eta.stage?.label ? `（${eta.stage.label}）` : ''}` : batch.data?.status === 'paused' ? '已暂停，继续后更新当前阶段估计' : '当前阶段剩余时间暂无法估计'}。仅估计已排队的当前阶段；后续表区处理、精度升级、模型切换、导出和人工复核另计。</Typography.Paragraph>}
        <Space wrap>{batch.data?.capabilities.map(action => <Button key={action} loading={control.isPending} onClick={() => control.mutate(action)}>{({ pause: '暂停', resume: '继续', cancel: '取消', retry_failed: '仅重试失败项' } as Record<string, string>)[action] || action}</Button>)}
          <Button disabled={!selected.length || control.isPending} onClick={() => control.mutate('upgrade')}>对所选图片提高精度（{selected.length}）</Button>
          <Button onClick={() => void batch.refetch()}>刷新状态</Button><Checkbox checked={includeReview} onChange={e => setIncludeReview(e.target.checked)}>包含待复核文件</Checkbox>
          <Button type="primary" disabled={!counts.completed && !(includeReview && counts.needs_review)} loading={publish.isPending} onClick={() => publish.mutate()}>下载逐图 Excel ZIP</Button></Space></>}
      {publish.data && <><Space wrap>{publish.data.files.filter(file => file.name.endsWith('.zip')).map(file => <a key={file.name} href={`/api/platform/v1/workspaces/${workspaceId}/artifacts/${publish.data!.bundle_id}/files/${encodeURIComponent(file.name)}`}>{exportFreshness.data?.current === false ? '下载历史 Excel ZIP' : '下载 Excel ZIP'}</a>)}<Tag>{exportFreshness.data?.current === true ? '当前成果' : exportFreshness.data?.current === false ? '历史成果' : '成果版本待核实'}</Tag><Link to={`/artifacts?workspace=${workspaceId}&bundle=${publish.data.bundle_id}`}>查看交付清单</Link></Space>
        {exportFreshness.data?.current === false && <Alert type="warning" title="该 ZIP 为历史版本，请重新导出当前修订" description="表格、业务说明、候选或人工确认发生变化，文件数量相同也需要刷新当前成果。" />}</>}
      <div className="section-head"><Typography.Title level={4}>进度与集中复核</Typography.Title><Select aria-label="批次状态筛选" allowClear placeholder="全部图片" value={status} onChange={value => { setStatus(value); setPage(1) }} options={['needs_review', 'failed', 'completed', 'pending'].map(value => ({ value, label: labels[value] }))} /></div>
      <Table<Item> size="small" rowKey="input_version_id" dataSource={batch.data?.items || []} loading={batch.isPending} rowSelection={{ selectedRowKeys: selected, preserveSelectedRowKeys: true, onChange: keys => setSelected(keys.map(String)), getCheckboxProps: item => ({ disabled: !['completed', 'needs_review', 'failed', 'cancelled'].includes(item.status) }) }} pagination={{ current: page, pageSize: 30, total: batch.data?.filtered_total ?? batch.data?.total ?? counts?.total, showSizeChanger: false, onChange: setPage }}
        columns={[{ title: '原图', dataIndex: 'name', ellipsis: true }, { title: '状态', render: (_, item) => <Tag>{labels[item.status] || item.status}</Tag> }, { title: '阶段', dataIndex: 'stage' },
          { title: '疑点', render: (_, item) => item.issues?.slice(0, 3).map(issue => issue.message || issue.reason || issue.code).join('；') },
          { title: '操作', render: (_, item) => item.result_id ? <Button type="link" onClick={() => { setEdits({}); setMetadataDrafts({}); setDraftBinding(undefined); setCrop(undefined); setCandidate(undefined); setDiagnostic(undefined); setReviewNotice(''); comparison.reset(); save.reset(); setReview(item) }}>核对原图与结果</Button> : <Typography.Text type="secondary">尚无结果</Typography.Text> }]} locale={{ emptyText: <Empty description="当前筛选没有图片" /> }} />
    </>}
    {[start.error, batch.error, control.error, publish.error].filter(Boolean).map((error, index) => <Alert key={index} type="error" title={(error as Error).message} />)}
    <Modal title={review ? `核对：${review.name}` : '集中复核'} open={!!review} width={1000} onCancel={closeReview} footer={<Space><Button disabled={!detail.data || detail.isFetching || save.isPending || staleDraft || hasDraft} onClick={() => save.mutate('deferred')}>稍后复核</Button><Button disabled={!detail.data || detail.isFetching || save.isPending || staleDraft} onClick={() => save.mutate('retain')}>保留当前结果</Button><Button type="primary" loading={save.isPending} disabled={!detail.data || detail.isFetching || staleDraft} onClick={() => save.mutate('accepted')}>保存并确认本图</Button></Space>}>
      {detail.data && <><Typography.Paragraph>{detail.data.issues?.map(issue => issue.message || issue.reason || issue.code).join('；')}</Typography.Paragraph>
        {reviewNotice && <Alert type="warning" title={reviewNotice} />}
        {hasDraft && <Button disabled={save.isPending} onClick={() => { setEdits({}); setMetadataDrafts({}); setCandidate(undefined); comparison.reset(); setReviewNotice('') }}>清除待保存修改</Button>}
        {staleDraft && <Alert type="warning" title="复核期间结果或人工确认已变化，请重新加载后核对" />}
        <Button disabled={save.isPending} onClick={async () => { const value = await detail.refetch(); if (value.data) { setEdits({}); setMetadataDrafts({}); setCandidate(undefined); comparison.reset(); setDraftBinding(detailBinding(value.data)) } }}>重新加载当前复核版本</Button>
        {diagnosticData && <ScreenDiagnostics data={diagnosticData} selected={currentDiagnostic?.id} busy={control.isPending || save.isPending || comparison.isPending}
          select={item => { setDiagnostic(item); setCrop(undefined) }} action={(item, name) => { setDiagnostic(item); setCrop(undefined); if (name === 'structure_compare') comparison.mutate(); if (name === 'precision_subbatch') control.mutate({ action: 'upgrade', version_ids: [detail.data!.input_version_id] }); if (name === 'request_better_source') setDiagnosticNotice('请补充这张图的清晰、完整照片；保留原结果，不猜测不可见内容。'); }} />}
        {diagnosticNotice && <Alert type="info" title={diagnosticNotice} />}
        {detail.data && batchId && diagnosticData && <LocalCandidates key={`${workspaceId}:${batchId}:${detail.data.input_version_id}`}
          workspaceId={workspaceId} batchId={batchId} inputVersionId={detail.data.input_version_id}
          resultId={detail.data.result_id} revision={currentResultRevision(detail.data)} diagnostic={currentDiagnostic}
          remainingBudget={diagnosticData.budget.remaining_seconds} primaryPending={!!batch.data?.counts.pending}
          currentValue={(detail.data.tables || detail.data.result?.tables || [])[currentDiagnostic?.location.table ?? -1]?.cells?.find(cell =>
            (cell.row ?? cell.row_index) === currentDiagnostic?.location.row && (cell.column ?? cell.col_index) === currentDiagnostic?.location.column)?.text} />}
        <Space><a href={detail.data.original_url} target="_blank" rel="noreferrer">打开原图</a><Select aria-label="复核图像区域" value={crop || detail.data.original_url} onChange={setCrop} options={[{ value: detail.data.original_url, label: '原始图片' }, ...(detail.data.preparation?.prepared_images || []).map((image, index) => ({ value: image.url, label: `业务表区 ${index + 1}` }))]} /></Space>
        <div style={{ position: 'relative', margin: '12px 0' }}><img className="ocr-review-image" style={{ margin: 0 }} src={crop || detail.data.original_url} alt={detail.data.name} />
          {!crop && reliableBox && detail.data.preparation?.width && detail.data.preparation.height && <svg aria-label={`${diagnosticRange(currentDiagnostic!)}原图范围`} viewBox={`0 0 ${detail.data.preparation.width} ${detail.data.preparation.height}`} style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none' }}><rect x={reliableBox[0]} y={reliableBox[1]} width={reliableBox[2] - reliableBox[0]} height={reliableBox[3] - reliableBox[1]} fill="none" stroke="#b46c20" strokeWidth="3" /></svg>}</div>
        {!!detail.data.candidates?.length && <Space><Typography.Text>候选结果</Typography.Text><Select aria-label="复核候选结果" value={candidate || 'current'} style={{ minWidth: 220 }}
          disabled={Object.keys(metadataDrafts).length > 0 || staleDraft || save.isPending} onChange={value => { setCandidate(value === 'current' ? undefined : value); comparison.reset(); setEdits({}) }} options={[{ value: 'current', label: '当前最终结果' }, ...detail.data.candidates.filter(value => value.result_id).map((value, index) => ({ value: value.result_id!, label: `候选 ${index + 1} · ${value.engine || '已保留结果'}` }))]} />
          <Button disabled={!candidate || Object.keys(edits).length > 0} loading={comparison.isPending} onClick={() => comparison.mutate()}>比较候选差异</Button></Space>}
        {validComparison && <Typography.Paragraph>候选比较发现 {validComparison.conflicts.length} 项分歧；仅展示建议，未采用或修改结果。采用仍通过下方人工保存动作。</Typography.Paragraph>}
        {comparison.error && <Alert type="error" title={comparison.error.message} />}
        {candidate && <Alert type="info" title="切换整图候选后，保存并重新加载业务说明再编辑" />}
        <BusinessMetadata items={detail.data.business_metadata || []} drafts={metadataDrafts} disabled={!!candidate || detail.isFetching || staleDraft || save.isPending} change={(index, draft) => setMetadataDrafts(old => ({ ...old, [index]: draft }))} />
        <Typography.Paragraph type="secondary">默认按文本导出，保留编号、前导零和精确原值。保存并确认当前结果后，可选择金额、数量或比例列按数值导出；长编号仍按文本保留。</Typography.Paragraph>
        {tables.map((table, tableIndex) => <Space key={tableIndex} className="ocr-numeric-columns"><Typography.Text>表 {tableIndex + 1}：数值列</Typography.Text>
          <Select mode="multiple" allowClear disabled={detail.data!.review_state !== 'accepted' || detail.data!.review_stale || detail.data!.human_confirmed === false || (detail.data!.result_revision !== undefined && detail.data!.result_revision !== currentResultRevision(detail.data)) || !!candidate || Object.keys(edits).length > 0 || Object.keys(metadataDrafts).length > 0} aria-label={`表${tableIndex + 1}已确认数值列`} style={{ minWidth: 260 }} value={numericColumns[detail.data!.input_version_id]?.[String(tableIndex)] || []}
            options={Array.from({ length: table.columns ?? (table.cells || []).reduce((largest, cell) => Math.max(largest, (cell.column ?? cell.col_index ?? 0) + 1), 0) }, (_, column) => ({ value: column, label: `第 ${column + 1} 列` }))}
            onChange={columns => { const versionId = detail.data!.input_version_id; setNumericByBatch(old => ({ ...old, [batchId!]: { ...(old[batchId!] || {}), [versionId]: { ...(old[batchId!]?.[versionId] || {}), [String(tableIndex)]: columns } } })) }} /></Space>)}
        <Table size="small" rowKey="key" dataSource={cells} pagination={{ pageSize: 20 }} columns={[{ title: '表', dataIndex: 'table_index', render: value => value + 1 }, { title: '行', dataIndex: 'row', render: value => value + 1 }, { title: '列', dataIndex: 'column', render: value => value + 1 },
          { title: '当前值', dataIndex: 'before' }, { title: '复核值', render: (_, cell) => <Input disabled={detail.isFetching || staleDraft || save.isPending} aria-label={`表${cell.table_index + 1}行${cell.row + 1}列${cell.column + 1}`} value={edits[cell.key] ?? cell.before} onChange={e => setEdits(old => ({ ...old, [cell.key]: e.target.value }))} /> }]} />
        {!!detail.data.candidates?.length && <Typography.Paragraph type="secondary">已保留 {detail.data.candidates.length} 个候选与来源；确认仅作用于本图当前修订。</Typography.Paragraph>}</>}
      {(detail.error || save.error) && <Alert type="error" title={(detail.error || save.error)?.message} />}
    </Modal>
  </section>
}
