import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Checkbox, Input, InputNumber, Select, Space, Table, Typography } from 'antd'
import { Link } from 'react-router-dom'
import { assets, post, request, type Template } from './api'

const { Title, Text, Paragraph } = Typography
type Side = 'left' | 'right'
type Column = { column_id: string; header: string | null }
type Cell = { kind: string; value: string | boolean | null; raw: string | null }
type InputTable = { revision: string; name: string; rows: number; columns: Column[]; upload_id: string; format: string; sheets: string[]; options: Record<string, unknown>; history: { revision: string; step?: { operator: string } | null }[] }
type Job = { id: string; action: string; status: string; error?: string }
type Policy = { key_pairs: string[][]; compare_pairs: string[][]; amount_pairs: string[][]; type_policy: string; null_policy: string; amount_tolerance: string; trim_text: boolean; casefold_text: boolean }
type Session = { id: string; name: string; version: number; jobs: Job[]; state: { tables: Partial<Record<Side, InputTable>>; result_id: string | null; comparison?: { left_revision: string; right_revision: string; spec: Policy } } }
type Pair = { left: string; right: string; mode: 'ignore' | 'key' | 'compare'; amount: boolean }
type Draft = { sessionId?: string; left?: string; right?: string; pairs: Pair[]; typePolicy: string; nullPolicy: string; tolerance: string; trim: boolean; fold: boolean; checked: string }
const empty: Draft = { pairs: [], typePolicy: 'strict', nullPolicy: 'never_match', tolerance: '0', trim: false, fold: false, checked: '' }
const draftKey = (workspaceId: string) => `clear-first-data-setup:${workspaceId}`
const active = ['queued', 'running', 'cancelling']
function restored(workspaceId: string): Draft {
  try { const value = JSON.parse(localStorage.getItem(draftKey(workspaceId)) || 'null'); return Array.isArray(value?.pairs) && typeof value.tolerance === 'string' ? { ...empty, ...value, checked: '' } : empty } catch { return empty }
}
const label = (column: Column) => column.header || `${column.column_id}（无列名，须在数据工作台修正）`
const columnOptions = (table: InputTable) => table.columns.map(column => ({ value: column.column_id, label: label(column) }))
function samePolicy(left: Policy, right: Policy) { return Object.keys(right).every(key => JSON.stringify(left[key as keyof Policy]) === JSON.stringify(right[key as keyof Policy])) }

function TableSample({ sessionId, table, side }: { sessionId: string; table: InputTable; side: Side }) {
  const preview = useQuery({ queryKey: ['template-first-preview', sessionId, table.revision],
    queryFn: ({ signal }) => request<{ rows: { row_id: string; source_row: number; cells: Cell[] }[] }>(`/data/sessions/${encodeURIComponent(sessionId)}/tables/${encodeURIComponent(table.revision)}?offset=0&limit=5`, { signal }) })
  return <div><Title level={4}>{side === 'left' ? '左表' : '右表'}：{table.name}</Title><Paragraph type="secondary">{table.rows} 行 · {table.format.toUpperCase()} · 前 5 行</Paragraph>
    {preview.error && <Alert type="error" title={preview.error.message} />}
    <Table size="small" rowKey="row_id" dataSource={preview.data?.rows || []} loading={preview.isPending} pagination={false} scroll={{ x: 'max-content' }}
      columns={[{ title: '原行', dataIndex: 'source_row' }, ...table.columns.map((column, index) => ({ title: label(column), key: column.column_id, render: (_: unknown, row: { cells: Cell[] }) => {
        const cell = row.cells[index]; return <span title={`类型 ${cell.kind} · 原值 ${cell.raw ?? 'NULL'}`}>{cell.value === null ? cell.kind === 'unknown_formula' ? '公式结果未知' : 'NULL' : cell.value === '' ? '空字符串' : String(cell.value)}</span>
      } }))]} /></div>
}
export function FirstDataSetup({ workspaceId, name, onSaved, existingSessionId }: { workspaceId: string; name: string; onSaved: (item: Template) => void; existingSessionId?: string }) {
  const client = useQueryClient()
  const [draft, setDraft] = useState(() => restored(workspaceId))
  const [initialized, setInitialized] = useState('')
  const [options, setOptions] = useState<Record<Side, { header_row: number; encoding: string; delimiter: string; sheet_name?: string }>>({ left: { header_row: 1, encoding: 'utf-8-sig', delimiter: ',' }, right: { header_row: 1, encoding: 'utf-8-sig', delimiter: ',' } })
  const [cleanSide, setCleanSide] = useState<Side>('left'), [cleanColumn, setCleanColumn] = useState<string>(), [cleanOperator, setCleanOperator] = useState('trim')
  const [stage, setStage] = useState('')
  useEffect(() => { localStorage.setItem(draftKey(workspaceId), JSON.stringify(draft)) }, [draft, workspaceId])
  useEffect(() => { if (existingSessionId) { setDraft({ ...empty, sessionId: existingSessionId }); setInitialized('') } }, [existingSessionId])
  const library = useQuery({ queryKey: ['batch-assets', workspaceId], queryFn: ({ signal }) => assets(workspaceId, signal) })
  const session = useQuery({ queryKey: ['template-first-session', workspaceId, draft.sessionId], enabled: !!draft.sessionId,
    queryFn: ({ signal }) => request<Session>(`/data/sessions/${encodeURIComponent(draft.sessionId!)}`, { signal }),
    refetchInterval: query => query.state.data?.jobs.some(job => active.includes(job.status)) ? 1000 : false })
  const current = session.data
  const left = current?.state.tables.left, right = current?.state.tables.right
  useEffect(() => {
    if (!current || !left || !right || initialized === current.id) return
    const prior = current.state.comparison
    const spec = prior?.left_revision === left.revision && prior?.right_revision === right.revision ? prior.spec : null
    setDraft(value => ({ ...value, pairs: value.pairs.length ? value.pairs : left.columns.map(column => {
      const key = spec?.key_pairs.find(pair => pair[0] === column.column_id), compared = spec?.compare_pairs.find(pair => pair[0] === column.column_id)
      return { left: column.column_id, right: key?.[1] || compared?.[1] || '', mode: key ? 'key' : compared ? 'compare' : 'ignore', amount: !!spec?.amount_pairs.some(pair => pair[0] === column.column_id) }
    }), ...(spec && !value.pairs.length ? { typePolicy: spec.type_policy, nullPolicy: spec.null_policy, tolerance: spec.amount_tolerance, trim: spec.trim_text, fold: spec.casefold_text } : {}) }))
    setInitialized(current.id)
  }, [current?.id, left?.revision, right?.revision, initialized])
  function update(value: Partial<Draft>) { setDraft(old => ({ ...old, ...value, checked: '' })) }
  async function awaitJob(sessionId: string, job: Job) {
    let value = job
    while (active.includes(value.status)) {
      await new Promise(resolve => window.setTimeout(resolve, 700))
      value = await request<Job>(`/data/sessions/${encodeURIComponent(sessionId)}/jobs/${encodeURIComponent(job.id)}`)
    }
    if (value.status !== 'succeeded') throw new Error(value.error || `操作${value.status}，已保留最后成功版本`)
    const latest = await request<Session>(`/data/sessions/${encodeURIComponent(sessionId)}`)
    client.setQueryData(['template-first-session', workspaceId, sessionId], latest)
    return latest
  }
  async function action(work: Session, kind: string, payload: Record<string, unknown>) {
    setStage(kind === 'import' ? '导入并读取实际列名' : kind === 'step' ? '应用明确清洗步骤' : '按明确规则进行核对')
    const job = await post<Job>(`/data/sessions/${encodeURIComponent(work.id)}/actions`, { action: kind, payload, version: work.version })
    return awaitJob(work.id, job)
  }
  const prepare = useMutation({ mutationFn: async () => {
    if (!draft.left || !draft.right || draft.left === draft.right) throw new Error('明确选择两份不同的样本资料')
    let work = draft.sessionId ? await request<Session>(`/data/sessions/${encodeURIComponent(draft.sessionId)}`) : await post<Session>('/data/sessions', { name: name.trim() || '首次两表规则核对' })
    setDraft(value => ({ ...value, sessionId: work.id, checked: '' }))
    client.setQueryData(['template-first-session', workspaceId, work.id], work)
    const selection = await post<{ asset_set_id: string; revision: number; items: { item_id: string; asset_id: string }[] }>(`/workspaces/${encodeURIComponent(workspaceId)}/assets/sets`, { asset_ids: [draft.left, draft.right] })
    for (const side of ['left', 'right'] as const) {
      if (work.state.tables[side]) continue
      const member = selection.items.find(item => item.asset_id === draft[side])!
      setStage(`准备${side === 'left' ? '左表' : '右表'}原件`)
      const source = await post<{ id: string }>(`/data/sessions/${encodeURIComponent(work.id)}/asset-uploads`, { workspace_id: workspaceId, asset_set_id: selection.asset_set_id, asset_set_revision: selection.revision, item_id: member.item_id })
      work = await action(work, 'import', { side, upload_id: source.id, options: options[side] })
    }
    return work
  }, onSettled: () => setStage('') })
  const operate = useMutation({ mutationFn: async (value: { kind: string; payload: Record<string, unknown> }) => {
    if (!draft.sessionId) throw new Error('先导入样本资料')
    update({})
    return action(await request<Session>(`/data/sessions/${encodeURIComponent(draft.sessionId)}`), value.kind, value.payload)
  }, onSettled: () => setStage('') })
  const spec: Policy = { key_pairs: draft.pairs.filter(pair => pair.mode === 'key').map(pair => [pair.left, pair.right]), compare_pairs: draft.pairs.filter(pair => pair.mode === 'compare').map(pair => [pair.left, pair.right]),
    amount_pairs: draft.pairs.filter(pair => pair.mode === 'compare' && pair.amount).map(pair => [pair.left, pair.right]), type_policy: draft.typePolicy, null_policy: draft.nullPolicy, amount_tolerance: draft.tolerance, trim_text: draft.trim, casefold_text: draft.fold }
  const compared = !!left && !!right && !!current?.state.comparison && current.state.comparison.left_revision === left.revision && current.state.comparison.right_revision === right.revision && samePolicy(current.state.comparison.spec, spec)
  const result = useQuery({ queryKey: ['template-first-result', current?.id, current?.state.result_id], enabled: !!current?.state.result_id && compared,
    queryFn: ({ signal }) => request<{ total: number; rows: Record<string, unknown>[]; metadata: { coverage: Record<string, number> } }>(`/data/sessions/${encodeURIComponent(current!.id)}/results/${encodeURIComponent(current!.state.result_id!)}?offset=0&limit=10`, { signal }) })
  const token = compared && current?.state.result_id ? JSON.stringify([current.id, left!.revision, right!.revision, current.state.result_id, spec]) : ''
  const save = useMutation({ mutationFn: () => post<Template>('/batch-templates/from-data-session', { schema_version: '1.0', workspace_id: workspaceId, name: name.trim(), session_id: current!.id }),
    onSuccess: item => { onSaved(item); void client.invalidateQueries({ queryKey: ['batch-templates', workspaceId] }) } })
  const busy = prepare.isPending || operate.isPending || save.isPending || !!current?.jobs.some(job => active.includes(job.status))
  const pairsValid = !!left && !!right && draft.pairs.some(pair => pair.mode === 'key') && draft.pairs.every(pair => pair.mode === 'ignore' || !!pair.right) && [...left.columns, ...right.columns].every(column => column.header)
  const choices = library.data?.filter(asset => /\.(csv|xlsx|parquet)$/i.test(asset.original_name)).map(asset => ({ value: asset.asset_id, label: `${asset.original_name} · 版本 ${asset.revision}` }))
  const cleanTable = current?.state.tables[cleanSide]
  return <div className="batch-first-setup"><Title level={3}>首次两表连续配置</Title><Paragraph>先用两份小样本资料核对规则。导入、清洗、显式匹配、结果核对和保存均可在本页完成；资料可通过上方上传队列登记。</Paragraph>
    {!current?.state.tables.left || !current?.state.tables.right ? <><div className="batch-template-fields">{(['left', 'right'] as const).map(side => <div key={side}><label>{side === 'left' ? '左表样本' : '右表样本'}<Select aria-label={`首配${side === 'left' ? '左' : '右'}表资料`} value={draft[side]} disabled={busy || !!draft.sessionId} options={choices} onChange={value => update({ [side]: value })} /></label>
      <Space wrap><InputNumber aria-label={`${side}首配表头行`} min={1} max={1000} value={options[side].header_row} onChange={value => setOptions(old => ({ ...old, [side]: { ...old[side], header_row: value || 1 } }))} />
        <Select aria-label={`${side}首配编码`} value={options[side].encoding} onChange={value => setOptions(old => ({ ...old, [side]: { ...old[side], encoding: value } }))} options={['utf-8-sig', 'utf-8', 'gb18030', 'utf-16'].map(value => ({ value, label: value }))} />
        <Input aria-label={`${side}首配工作表`} placeholder="Excel 工作表名（可选）" value={options[side].sheet_name || ''} onChange={event => setOptions(old => ({ ...old, [side]: { ...old[side], sheet_name: event.target.value || undefined } }))} /></Space></div>)}</div>
      <Button disabled={busy || !draft.left || !draft.right || draft.left === draft.right} loading={prepare.isPending} onClick={() => prepare.mutate()}>连续导入左右样本</Button></> : <>
      <div className="batch-sample-tables"><TableSample sessionId={current.id} table={left!} side="left" /><TableSample sessionId={current.id} table={right!} side="right" /></div>
      <Title level={4}>确认清洗步骤</Title><Space wrap><Select aria-label="首配清洗角色" value={cleanSide} disabled={busy} onChange={value => { setCleanSide(value); setCleanColumn(undefined) }} options={[{ value: 'left', label: '左表' }, { value: 'right', label: '右表' }]} />
        <Select aria-label="首配清洗列" value={cleanColumn} onChange={setCleanColumn} disabled={busy} placeholder="明确选择实际列" options={cleanTable ? columnOptions(cleanTable) : []} />
        <Select aria-label="首配清洗操作" value={cleanOperator} onChange={setCleanOperator} disabled={busy} options={[{ value: 'trim', label: '去首尾空白' }, { value: 'decimal', label: '转换为十进制金额' }]} />
        <Button disabled={busy || !cleanColumn} onClick={() => operate.mutate({ kind: 'step', payload: { side: cleanSide, operator: cleanOperator === 'trim' ? 'trim' : 'cast', parameters: { column: cleanColumn, ...(cleanOperator === 'decimal' ? { target: 'decimal' } : {}) } } })}>应用首配清洗步骤</Button></Space>
      <Title level={4}>明确匹配键与左右列映射</Title><Paragraph type="secondary">初始不选择匹配键、不按列位置配对。忽略的列不参与比较；金额列明确勾选并设置容差。</Paragraph>
      {draft.pairs.map((pair, index) => <div className="batch-setup-pair" key={pair.left}><Text>{left!.columns.find(column => column.column_id === pair.left)?.header || pair.left}</Text>
        <Select aria-label={`首配右侧映射${pair.left}`} placeholder="选择实际右侧列" value={pair.right || undefined} disabled={busy} options={columnOptions(right!)} onChange={value => update({ pairs: draft.pairs.map((item, position) => position === index ? { ...item, right: value } : item) })} />
        <Select aria-label={`首配列用途${pair.left}`} value={pair.mode} disabled={busy} options={[{ value: 'ignore', label: '忽略' }, { value: 'key', label: '业务匹配键' }, { value: 'compare', label: '比对值' }]} onChange={value => update({ pairs: draft.pairs.map((item, position) => position === index ? { ...item, mode: value } : item) })} />
        <Checkbox checked={pair.amount} disabled={busy || pair.mode !== 'compare'} onChange={event => update({ pairs: draft.pairs.map((item, position) => position === index ? { ...item, amount: event.target.checked } : item) })}>金额</Checkbox></div>)}
      <Space wrap><Select aria-label="首配类型政策" value={draft.typePolicy} disabled={busy} onChange={value => update({ typePolicy: value })} options={[{ value: 'strict', label: '严格区分类型' }, { value: 'text', label: '按文本比较' }]} />
        <Select aria-label="首配空值政策" value={draft.nullPolicy} disabled={busy} onChange={value => update({ nullPolicy: value })} options={[{ value: 'never_match', label: '空值不匹配' }, { value: 'equal', label: '空值视为相同' }]} />
        <Input aria-label="首配金额容差" value={draft.tolerance} disabled={busy} style={{ width: 130 }} onChange={event => update({ tolerance: event.target.value })} />
        <Checkbox checked={draft.trim} disabled={busy} onChange={event => update({ trim: event.target.checked })}>比较时去首尾空白</Checkbox>
        <Checkbox checked={draft.fold} disabled={busy} onChange={event => update({ fold: event.target.checked })}>忽略大小写</Checkbox>
        <Button type="primary" disabled={busy || !pairsValid || !/^\+?(?:\d+(?:\.\d*)?|\.\d+)$/.test(draft.tolerance)} onClick={() => operate.mutate({ kind: 'compare', payload: { spec } })}>执行样本规则核对</Button></Space>
      {current.state.result_id && !compared && <Paragraph type="secondary">清洗版本或比对规则已改变，重新执行核对后才能确认保存。</Paragraph>}
      {compared && result.data && <><Title level={4}>小样本核对结果</Title><Paragraph>输入按已选规则全量比较；这里展示前 {result.data.rows.length} 条，共 {result.data.total} 条结果。覆盖：{Object.entries(result.data.metadata.coverage).map(([key, count]) => `${({ same_pairs: '相同', different_pairs: '差异', only_left: '仅左', only_right: '仅右', ambiguous_keys: '重复键' } as Record<string, string>)[key] || key} ${count}`).join(' · ')}</Paragraph>
        <Table<Record<string, unknown>> size="small" rowKey={row => String(row.ordinal)} pagination={false} scroll={{ x: 'max-content' }} dataSource={result.data.rows}
          columns={[{ title: '分类', dataIndex: 'category' }, { title: '左原行', dataIndex: 'left_source_row' }, { title: '右原行', dataIndex: 'right_source_row' },
            ...(['left', 'right'] as const).flatMap(side => current.state.tables[side]!.columns.map(column => ({ title: `${side === 'left' ? '左' : '右'} · ${label(column)}`, dataIndex: `${side === 'left' ? 'L' : 'R'}_${column.column_id}_value`, render: (value: unknown) => value === null ? 'NULL' : String(value ?? '') })))]} />
        <Checkbox disabled={busy} checked={!!token && draft.checked === token} onChange={event => setDraft(old => ({ ...old, checked: event.target.checked ? token : '' }))}>已核对样本结果、匹配键、列映射及清洗政策</Checkbox>
        <Button type="primary" disabled={busy || !name.trim() || !token || draft.checked !== token} loading={save.isPending} onClick={() => save.mutate()}>确认并保存模板</Button></>}
    </>}
    {stage && <Paragraph role="status">{stage}，成功阶段会保留，可继续恢复。</Paragraph>}
    {[library.error, session.error, prepare.error, operate.error, result.error, save.error].filter(Boolean).map((error, index) => <Alert key={index} type="error" title={(error as Error).message} />)}
    <Space className="application-message" wrap>{draft.sessionId && <><Button disabled={busy} onClick={() => void session.refetch()}>刷新首配作业</Button>
      <Link to={`/apps/data?workspace=${encodeURIComponent(workspaceId)}&session=${encodeURIComponent(draft.sessionId)}`}>高级清洗与完整结果</Link></>}
      <Button disabled={busy} onClick={() => { setDraft(empty); setInitialized('') }}>开始新的首配</Button></Space>
  </div>
}
