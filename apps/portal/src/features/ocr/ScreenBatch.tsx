import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Checkbox, Empty, Input, Modal, Progress, Select, Space, Table, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'

type Item = { input_version_id: string; image_id: string; asset_id: string; name: string; status: string; stage: string; review_state?: string; result_id?: string; result_revision?: number; issues: { code?: string; message?: string; reason?: string }[] }
type Batch = { batch_id: string; status: string; counts: { total: number; completed: number; needs_review: number; failed: number; cancelled: number; pending: number }; items: Item[]; total?: number; filtered_total?: number; capabilities: string[] }
type Cell = { row?: number; column?: number; row_index?: number; col_index?: number; text?: string; content?: string }
type BusinessTable = { cells?: Cell[]; columns?: number; caption?: string }
type Detail = Item & { original_url: string; result?: { tables?: BusinessTable[] }; tables?: BusinessTable[]; candidates?: { result_id?: string; engine?: string; tables?: BusinessTable[] }[]; preparation?: { prepared_images?: { id: string; kind: string; url: string }[] } }
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
  const [mode, setMode] = useState('auto'), [status, setStatus] = useState<string>(), [page, setPage] = useState(1)
  const [includeReview, setIncludeReview] = useState(false), [review, setReview] = useState<Item | null>(null), [edits, setEdits] = useState<Record<string, string>>({})
  const [crop, setCrop] = useState<string>()
  const [candidate, setCandidate] = useState<string>()
  const [selected, setSelected] = useState<string[]>([])
  const [numericByBatch, setNumericByBatch] = useState<Record<string, NumericColumns>>({})
  const numericColumns = batchId ? numericByBatch[batchId] || {} : {}
  const base = `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/ocr/screen-batches`
  const batch = useQuery({ queryKey: ['ocr-screen-batch', workspaceId, batchId, page, status], enabled: !!batchId,
    queryFn: ({ signal }) => json<Batch>(`${base}/${batchId}?offset=${(page - 1) * 30}&limit=30${status ? `&status=${status}` : ''}`, { signal }),
    refetchInterval: q => q.state.data?.counts.pending ? 2500 : false })
  const refresh = () => void client.invalidateQueries({ queryKey: ['ocr-screen-batch', workspaceId] })
  const start = useMutation({ mutationFn: () => identified<Batch>(`clear-screen-submit:${workspaceId}:${setId}:${revision}`, base,
    { link_id: linkId, asset_set_id: setId, asset_set_revision: revision, policy: { mode } }),
    onSuccess: value => { const next = new URLSearchParams(params); next.set('screen_batch', value.batch_id); setParams(next); refresh() } })
  const control = useMutation({ mutationFn: (action: string) => identified<Batch>(`clear-screen-action:${workspaceId}:${batchId}`, `${base}/${batchId}/action`, { action, ...(action === 'upgrade' ? { version_ids: selected } : {}) }), onSuccess: value => {
    if (value.batch_id !== batchId) { const next = new URLSearchParams(params); next.set('screen_batch', value.batch_id); setParams(next); setSelected([]); setPage(1); setStatus(undefined) }
    refresh()
  } })
  const publish = useMutation({ mutationFn: () => json<Bundle>(`${base}/${batchId}/export`, post({ include_pending_review: includeReview, ...(Object.keys(numericColumns).length ? { numeric_columns: numericColumns } : {}) })) })
  const detail = useQuery({ queryKey: ['ocr-screen-detail', workspaceId, batchId, review?.input_version_id], enabled: !!review,
    queryFn: ({ signal }) => json<Detail>(`${base}/${batchId}/items/${review!.input_version_id}`, { signal }) })
  const tables = (candidate ? detail.data?.candidates?.find(value => value.result_id === candidate)?.tables : detail.data?.tables || detail.data?.result?.tables) || []
  const cells = tables.flatMap((table, tableIndex) => (table.cells || []).map(cell => ({
    key: `${tableIndex}:${cell.row ?? cell.row_index ?? 0}:${cell.column ?? cell.col_index ?? 0}`, table_index: tableIndex,
    row: cell.row ?? cell.row_index ?? 0, column: cell.column ?? cell.col_index ?? 0, before: cell.text ?? cell.content ?? '' })))
  const save = useMutation({ mutationFn: async (state: string) => {
    const selected = detail.data
    if (!selected?.result_id || selected.result_revision === undefined) throw new Error('尚无可核实的最终结果')
    return identified(`clear-screen-review:${workspaceId}:${batchId}:${selected.input_version_id}`, `${base}/${batchId}/action`, {
      action: 'review', version_ids: [selected.input_version_id], result_id: selected.result_id, expected_revision: selected.result_revision, state,
      ...(candidate ? { candidate_id: candidate } : {}),
      edits: cells.filter(cell => edits[cell.key] !== undefined && edits[cell.key] !== cell.before).map(({ key, ...cell }) => ({ ...cell, after: edits[key] })) })
  }, onSuccess: () => {
    if (batchId && review) setNumericByBatch(old => {
      const remaining = { ...(old[batchId] || {}) }; delete remaining[review.input_version_id]
      return { ...old, [batchId]: remaining }
    })
    setReview(null); setEdits({}); refresh()
  } })
  const counts = batch.data?.counts
  return <section className="panel ocr-screen-batch"><div className="section-head"><Typography.Title level={3}>批量图片转 Excel</Typography.Title><Tag>每张图片一个文件</Tag></div>
    {!batchId ? <><Typography.Paragraph type="secondary">一次处理最多 1000 张图片，先定位业务表区，再识别和集中复核疑点，最后下载 ZIP。</Typography.Paragraph>
      <Space wrap><Typography.Text>{count} 张</Typography.Text><Select aria-label="批次处理策略" value={mode} onChange={setMode} options={[{ value: 'auto', label: '自动处理' }, { value: 'accuracy', label: '提高精度' }]} />
        <Button type="primary" disabled={!ready || !linkId || !count || count > 1000} loading={start.isPending} onClick={() => start.mutate()}>开始整批转 Excel</Button></Space></> : <>
      {counts && <><Space wrap>{(['total', 'completed', 'pending', 'needs_review', 'failed', 'cancelled'] as const).map(key => <Typography.Text key={key}>{({ total: '总数', completed: '完成', pending: '处理中', needs_review: '待复核', failed: '失败', cancelled: '取消' })[key]} {counts[key]}</Typography.Text>)}<Tag>{labels[batch.data!.status] || batch.data!.status}</Tag></Space>
        <Progress percent={Math.round((counts.total - counts.pending) * 100 / Math.max(counts.total, 1))} status={counts.pending ? 'active' : counts.failed ? 'exception' : 'normal'} />
        <Space wrap>{batch.data?.capabilities.map(action => <Button key={action} loading={control.isPending} onClick={() => control.mutate(action)}>{({ pause: '暂停', resume: '继续', cancel: '取消', retry_failed: '仅重试失败项' } as Record<string, string>)[action] || action}</Button>)}
          <Button disabled={!selected.length || control.isPending} onClick={() => control.mutate('upgrade')}>对所选图片提高精度（{selected.length}）</Button>
          <Button onClick={() => void batch.refetch()}>刷新状态</Button><Checkbox checked={includeReview} onChange={e => setIncludeReview(e.target.checked)}>包含待复核文件</Checkbox>
          <Button type="primary" disabled={!counts.completed && !(includeReview && counts.needs_review)} loading={publish.isPending} onClick={() => publish.mutate()}>下载逐图 Excel ZIP</Button></Space></>}
      {publish.data && <Space wrap>{publish.data.files.filter(file => file.name.endsWith('.zip')).map(file => <a key={file.name} href={`/api/platform/v1/workspaces/${workspaceId}/artifacts/${publish.data!.bundle_id}/files/${encodeURIComponent(file.name)}`}>下载 Excel ZIP</a>)}<Link to={`/artifacts?workspace=${workspaceId}&bundle=${publish.data.bundle_id}`}>查看交付清单</Link></Space>}
      <div className="section-head"><Typography.Title level={4}>进度与集中复核</Typography.Title><Select aria-label="批次状态筛选" allowClear placeholder="全部图片" value={status} onChange={value => { setStatus(value); setPage(1) }} options={['needs_review', 'failed', 'completed', 'pending'].map(value => ({ value, label: labels[value] }))} /></div>
      <Table<Item> size="small" rowKey="input_version_id" dataSource={batch.data?.items || []} loading={batch.isPending} rowSelection={{ selectedRowKeys: selected, preserveSelectedRowKeys: true, onChange: keys => setSelected(keys.map(String)), getCheckboxProps: item => ({ disabled: !['completed', 'needs_review', 'failed', 'cancelled'].includes(item.status) }) }} pagination={{ current: page, pageSize: 30, total: batch.data?.filtered_total ?? batch.data?.total ?? counts?.total, showSizeChanger: false, onChange: setPage }}
        columns={[{ title: '原图', dataIndex: 'name', ellipsis: true }, { title: '状态', render: (_, item) => <Tag>{labels[item.status] || item.status}</Tag> }, { title: '阶段', dataIndex: 'stage' },
          { title: '疑点', render: (_, item) => item.issues?.slice(0, 3).map(issue => issue.message || issue.reason || issue.code).join('；') },
          { title: '操作', render: (_, item) => item.result_id ? <Button type="link" onClick={() => { setEdits({}); setCrop(undefined); setCandidate(undefined); save.reset(); setReview(item) }}>核对原图与结果</Button> : <Typography.Text type="secondary">尚无结果</Typography.Text> }]} locale={{ emptyText: <Empty description="当前筛选没有图片" /> }} />
    </>}
    {[start.error, batch.error, control.error, publish.error].filter(Boolean).map((error, index) => <Alert key={index} type="error" title={(error as Error).message} />)}
    <Modal title={review ? `核对：${review.name}` : '集中复核'} open={!!review} width={1000} onCancel={() => setReview(null)} footer={<Space><Button disabled={!detail.data || save.isPending} onClick={() => save.mutate('deferred')}>稍后复核</Button><Button disabled={!detail.data || save.isPending} onClick={() => save.mutate('retain')}>保留当前结果</Button><Button type="primary" loading={save.isPending} disabled={!detail.data} onClick={() => save.mutate('accepted')}>保存并确认本图</Button></Space>}>
      {detail.data && <><Typography.Paragraph>{detail.data.issues?.map(issue => issue.message || issue.reason || issue.code).join('；')}</Typography.Paragraph>
        <Space><a href={detail.data.original_url} target="_blank" rel="noreferrer">打开原图</a><Select aria-label="复核图像区域" value={crop || detail.data.original_url} onChange={setCrop} options={[{ value: detail.data.original_url, label: '原始图片' }, ...(detail.data.preparation?.prepared_images || []).map((image, index) => ({ value: image.url, label: `业务表区 ${index + 1}` }))]} /></Space>
        <img className="ocr-review-image" src={crop || detail.data.original_url} alt={detail.data.name} />
        {!!detail.data.candidates?.length && <Space><Typography.Text>候选结果</Typography.Text><Select aria-label="复核候选结果" value={candidate || 'current'} style={{ minWidth: 220 }}
          onChange={value => { setCandidate(value === 'current' ? undefined : value); setEdits({}) }} options={[{ value: 'current', label: '当前最终结果' }, ...detail.data.candidates.filter(value => value.result_id && value.tables?.length).map((value, index) => ({ value: value.result_id!, label: `候选 ${index + 1} · ${value.engine || '已保留结果'}` }))]} /></Space>}
        <Typography.Paragraph type="secondary">默认按文本导出，保留编号、前导零和精确原值。保存并确认当前结果后，可选择金额、数量或比例列按数值导出；长编号仍按文本保留。</Typography.Paragraph>
        {tables.map((table, tableIndex) => <Space key={tableIndex} className="ocr-numeric-columns"><Typography.Text>表 {tableIndex + 1}：数值列</Typography.Text>
          <Select mode="multiple" allowClear disabled={detail.data!.review_state !== 'accepted' || !!candidate || Object.keys(edits).length > 0} aria-label={`表${tableIndex + 1}已确认数值列`} style={{ minWidth: 260 }} value={numericColumns[detail.data!.input_version_id]?.[String(tableIndex)] || []}
            options={Array.from({ length: table.columns ?? (table.cells || []).reduce((largest, cell) => Math.max(largest, (cell.column ?? cell.col_index ?? 0) + 1), 0) }, (_, column) => ({ value: column, label: `第 ${column + 1} 列` }))}
            onChange={columns => { const versionId = detail.data!.input_version_id; setNumericByBatch(old => ({ ...old, [batchId!]: { ...(old[batchId!] || {}), [versionId]: { ...(old[batchId!]?.[versionId] || {}), [String(tableIndex)]: columns } } })) }} /></Space>)}
        <Table size="small" rowKey="key" dataSource={cells} pagination={{ pageSize: 20 }} columns={[{ title: '表', dataIndex: 'table_index', render: value => value + 1 }, { title: '行', dataIndex: 'row', render: value => value + 1 }, { title: '列', dataIndex: 'column', render: value => value + 1 },
          { title: '当前值', dataIndex: 'before' }, { title: '复核值', render: (_, cell) => <Input aria-label={`表${cell.table_index + 1}行${cell.row + 1}列${cell.column + 1}`} value={edits[cell.key] ?? cell.before} onChange={e => setEdits(old => ({ ...old, [cell.key]: e.target.value }))} /> }]} />
        {!!detail.data.candidates?.length && <Typography.Paragraph type="secondary">已保留 {detail.data.candidates.length} 个候选与来源；确认仅作用于本图当前修订。</Typography.Paragraph>}</>}
      {(detail.error || save.error) && <Alert type="error" title={(detail.error || save.error)?.message} />}
    </Modal>
  </section>
}
