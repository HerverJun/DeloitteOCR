import { ToolIdentity } from '../catalog/ToolIdentity'
import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Checkbox, Empty, Input, InputNumber, Modal, Pagination, Select, Space, Spin, Table, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { brands, applicationDescriptions } from '../../clear-ui/theme'
import { ApplicationDeclaration, dataDeclaration } from '../catalog/ApplicationDeclaration'
import { useWorkspaces } from '../../App'
import './data.css'

const { Title, Text, Paragraph } = Typography
const API = '/api/platform/v1/data'
const active = ['queued', 'running', 'cancelling']
const operatorNames: Record<string, string> = { source: '原始输入', trim: '去除首尾空白', rename: '列改名', map: '值映射', cast: '类型转换', filter: '筛选', duplicates: '识别重复', filter_compare: '类型化区间筛选', group_summary: '分组计数与金额合计' }
const categoryNames: Record<string, string> = { same: '相同', different: '差异', only_left: '仅左', only_right: '仅右', ambiguous_left: '重复键·左', ambiguous_right: '重复键·右' }
const jobNames: Record<string, string> = { import: '导入', step: '清洗', undo: '撤销', select_revision: '切换版本', save_recipe: '保存配方', replay: '应用配方', compare: '比对', export: '导出', export_table: '导出当前表' }
const jobStatusNames: Record<string, string> = { queued: '等待执行', running: '进行中', cancelling: '正在取消', succeeded: '已完成', failed: '失败', cancelled: '已取消' }
type Side = 'left' | 'right'
type Column = { column_id: string; header: string | null }
type Cell = { kind: string; value: string | boolean | null; raw: string | null; warning?: string | null }
type Revision = { revision: string; step: { operator: string; parameters: Record<string, unknown> } | null; input_rows: number; output_rows: number; errors_total: number }
type InputTable = { revision: string; name: string; rows: number; columns: Column[]; upload_id: string; format: string; sheets: string[]; warnings: string[]; options: ImportOptions; history: Revision[]; versions?: Revision[] }
type ImportOptions = { sheet_name?: string; header_row?: number | null; encoding?: string; delimiter?: string }
type Recipe = { id: string; name: string; steps: unknown[]; columns: Column[] }
type Export = { id: string; result_id: string | null; files: { name: string; bytes: number; sha256: string }[] }
type Job = { id: string; action: string; status: string; error?: string | null }
type Policy = { key_pairs: string[][]; compare_pairs: string[][]; amount_pairs: string[][]; type_policy: string; null_policy: string; amount_tolerance: string; trim_text: boolean; casefold_text: boolean }
type Session = { id: string; name: string; workspace_id?: string | null; version: number; updated: string; jobs: Job[]; state: { tables: Partial<Record<Side, InputTable>>; recipes: Recipe[]; result_id: string | null; comparison?: { left_revision: string; right_revision: string; spec: Policy }; comparisons: { id: string; left_name: string; right_name: string; coverage: Record<string, number> }[]; exports: Export[] } }
type DataRow = { ordinal: number; row_id: string; source_row: number; cells: Cell[]; source_rows?: { source_row: number }[] }
type Preview = { total: number; rows: DataRow[]; metadata: { event: { errors_total: number; error_sample: { code: string; raw: string; row_id: string }[] }; duplicate_groups: number } }
type ResultPage = { total: number; rows: (Record<string, string> & { ordinal: number })[]; metadata: { coverage: Record<string, number>; left: InputTable; right: InputTable } }
type Run = (action: string, payload: Record<string, unknown>) => Promise<void>
type AssetSelection = { asset_set_id: string; revision: number;
  items: { item_id: string; asset_id: string }[];
  members: { asset_id: string; original_name: string }[] }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(API + path, init)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : `操作未完成（${response.status}）`)
  }
  return response.json()
}
function post<T>(path: string, value: unknown) { return request<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) }) }
function label(column: Column) { return column.header || column.column_id }
function options(columns: Column[]) { return columns.map(column => ({ value: column.column_id, label: label(column) })) }

function FixedTable({ session }: { session: Session }) {
  const [params, setParams] = useSearchParams(), revision = params.get('table')
  const [page, setPage] = useState(Math.floor(Math.max(0, Number(params.get('offset')) || 0) / 20) + 1)
  useEffect(() => setPage(Math.floor(Math.max(0, Number(params.get('offset')) || 0) / 20) + 1), [revision])
  const preview = useQuery({ queryKey: ['data-fixed-table', session.id, revision, page], enabled: !!revision,
    queryFn: () => request<Preview & { metadata: { columns: Column[] } }>(`/sessions/${session.id}/tables/${revision}?offset=${(page - 1) * 20}&limit=20`) })
  if (!revision) return null
  return <section className="panel" aria-label="固定版本来源"><div className="section-head"><Title level={3}>固定版本来源</Title><Button onClick={() => { const next = new URLSearchParams(params); next.delete('table'); next.delete('offset'); setParams(next) }}>关闭来源预览</Button></div>
    <Paragraph type="secondary">这里读取证据引用时保存的表格版本。下方工具区可以继续处理当前版本。</Paragraph><Text code>{revision}</Text>
    {preview.error && <Alert type="error" title={preview.error.message} />}
    <Table<DataRow> size="small" loading={preview.isPending} rowKey="row_id" scroll={{ x: 'max-content' }} dataSource={preview.data?.rows || []} columns={[
      { title: '来源行', render: (_, row) => row.source_rows?.map(source => source.source_row).join('、') || row.source_row },
      ...(preview.data?.metadata.columns || []).map((column, index) => ({ title: label(column), key: column.column_id, render: (_: unknown, row: DataRow) => String(row.cells[index]?.value ?? '') })),
    ]} pagination={{ current: page, pageSize: 20, total: preview.data?.total || 0, showSizeChanger: false, onChange: setPage }} />
  </section>
}

export function DataApplicationCard() {
  const [params] = useSearchParams()
  const health = useQuery({ queryKey: ['data-health'], queryFn: () => request<{ ready: boolean }>('/health'), refetchInterval: 15_000 })
  const linked = `/apps/data${params.size ? `?${params}` : ''}`
  return <Card className="application-card" title={<ToolIdentity appId="data" to={linked} label={brands.data} />}>
    <Text className="application-purpose">{applicationDescriptions.data.purpose}</Text>
    <Paragraph className="application-description" type="secondary">{applicationDescriptions.data.description}</Paragraph>
    <div className="application-state"><Tag color={health.data?.ready ? 'success' : 'default'}>{health.isPending ? '正在检测' : health.isError ? '无法检测' : health.data?.ready ? '可使用' : '暂不可用'}</Tag><Text type="secondary">{health.data?.ready ? '本地 CPU 处理' : '可打开工作台查看环境状态'}</Text></div>
    <Link className="application-actions" to={linked}><Button type="primary">打开数据工作台</Button></Link>
    <ApplicationDeclaration declaration={dataDeclaration} />
  </Card>
}

function RecipeControls({ table, side, recipes, busy, run }: { table: InputTable; side: Side; recipes: Recipe[]; busy: boolean; run: Run }) {
  const [name, setName] = useState('我的清洗配方')
  const [chosen, setChosen] = useState<string>()
  const [open, setOpen] = useState(false)
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const recipe = recipes.find(item => item.id === chosen)
  const show = () => {
    if (!recipe) return
    setMapping(Object.fromEntries(recipe.columns.map(column => [column.column_id, table.columns.find(target => target.header === column.header)?.column_id || ''])))
    setOpen(true)
  }
  return <div className="data-recipe">
    <Space.Compact><Input aria-label={`${side}配方名称`} value={name} onChange={event => setName(event.target.value)} maxLength={100} />
      <Button disabled={busy || table.history.length < 2} onClick={() => void run('save_recipe', { side, name })}>保存配方</Button></Space.Compact>
    {recipes.length > 0 && <Space.Compact><Select aria-label={`${side}已保存配方`} placeholder="选择已保存配方" value={chosen} style={{ minWidth: 180 }} onChange={setChosen} options={recipes.map(item => ({ value: item.id, label: `${item.name} · ${item.steps.length} 步` }))} />
      <Button disabled={busy || !chosen} onClick={show}>应用到当前表</Button></Space.Compact>}
    <Modal title="确认配方列映射" open={open} okText="应用配方" cancelText="取消" onCancel={() => setOpen(false)}
      okButtonProps={{ disabled: busy || !recipe || recipe.columns.some(column => !mapping[column.column_id]) }}
      onOk={() => { if (recipe) void run('replay', { side, recipe_id: recipe.id, mapping }).then(() => setOpen(false)) }}>
      <Paragraph>将保存配方中的列对应到当前文件。原有版本会保留。</Paragraph>
      {recipe?.columns.map(column => <div className="data-mapping-row" key={column.column_id}><Text>{label(column)}</Text>
        <Select aria-label={`配方映射${column.column_id}`} value={mapping[column.column_id] || undefined} options={options(table.columns)} onChange={value => setMapping(old => ({ ...old, [column.column_id]: value }))} /></div>)}
    </Modal>
  </div>
}

function InputPanel({ session, side, busy, run, upload }: { session: Session; side: Side; busy: boolean; run: Run; upload: (side: Side, file: File, settings: ImportOptions) => Promise<void> }) {
  const table = session.state.tables[side]
  const [settings, setSettings] = useState<ImportOptions>({ header_row: 1, encoding: 'utf-8-sig', delimiter: ',' })
  const [operator, setOperator] = useState('trim')
  const [column, setColumn] = useState<string>()
  const [text, setText] = useState('')
  const [replacement, setReplacement] = useState('')
  const [target, setTarget] = useState('decimal')
  const [predicate, setPredicate] = useState('equals')
  const [typedPredicate, setTypedPredicate] = useState('ge'), [valueKind, setValueKind] = useState('decimal'), [upper, setUpper] = useState(''), [amountColumns, setAmountColumns] = useState<string[]>([])
  const [duplicates, setDuplicates] = useState<string[]>([])
  const [includeNull, setIncludeNull] = useState(false)
  const [page, setPage] = useState(1)
  useEffect(() => { setPage(1); if (table) { setSettings(table.options); setColumn(table.columns[0]?.column_id); setDuplicates([table.columns[0]?.column_id].filter(Boolean)) } }, [table?.revision])
  const preview = useQuery({ queryKey: ['data-preview', session.id, table?.revision, page], enabled: !!table,
    queryFn: () => request<Preview>(`/sessions/${session.id}/tables/${table!.revision}?offset=${(page - 1) * 20}&limit=20`) })
  const apply = () => {
    let parameters: Record<string, unknown> = { column }
    if (operator === 'rename') parameters.header = text
    if (operator === 'map') parameters.values = { [text]: replacement }
    if (operator === 'cast') parameters.target = target
    if (operator === 'filter') { parameters.predicate = predicate; if (!['is_null', 'not_null'].includes(predicate)) parameters.value = text }
    if (operator === 'duplicates') parameters = { columns: duplicates, include_null: includeNull }
    if (operator === 'filter_compare') parameters = { column, predicate: typedPredicate, value_kind: valueKind, value: text, ...(typedPredicate === 'between' ? { upper } : {}) }
    if (operator === 'group_summary') parameters = { columns: duplicates, amount_columns: amountColumns }
    void run('step', { side, operator, parameters })
  }
  return <section className="data-input panel">
    <div className="section-head"><Title level={3}>{side === 'left' ? '左表' : '右表'} <Text type="secondary">{table ? `${table.rows.toLocaleString()} 行` : '选择输入文件'}</Text></Title>
      <label className={`data-upload ${busy ? 'disabled' : ''}`}><input type="file" accept=".csv,.xlsx,.parquet" disabled={busy}
        aria-label={side === 'left' ? '上传左表' : '上传右表'} onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void upload(side, file, settings) }} />{table ? '更换文件' : '上传文件'}</label>
    </div>
    {table && <Paragraph ellipsis={{ rows: 1 }} title={table.name}><strong>{table.name}</strong> <Tag>{table.format.toUpperCase()}</Tag></Paragraph>}
    <details className="data-options" open={!table}><summary>导入设置 <span>表头 · 编码 · 工作表</span></summary><div className="data-import-settings">
      {table?.format === 'xlsx' && <Select aria-label={`${side}工作表`} value={settings.sheet_name} options={table.sheets.map(name => ({ value: name, label: name }))} onChange={sheet_name => setSettings(old => ({ ...old, sheet_name }))} />}
      {(!table || table.format === 'csv') && <><Select aria-label={`${side}编码`} value={settings.encoding || 'utf-8-sig'} onChange={encoding => setSettings(old => ({ ...old, encoding }))}
        options={['utf-8-sig', 'utf-8', 'gb18030', 'utf-16'].map(value => ({ value, label: value }))} />
        <Select aria-label={`${side}分隔符`} value={settings.delimiter || ','} options={[{ value: ',', label: '逗号' }, { value: '\t', label: '制表符' }, { value: ';', label: '分号' }, { value: '|', label: '竖线' }]} onChange={delimiter => setSettings(old => ({ ...old, delimiter }))} /></>}
      {table?.format !== 'parquet' && <label>表头行 <InputNumber aria-label={`${side}表头行`} min={0} max={1000} value={settings.header_row ?? 0} onChange={value => setSettings(old => ({ ...old, header_row: value === 0 ? null : value ?? 1 }))} /></label>}
      {table && <Button disabled={busy} onClick={() => void run('import', { side, upload_id: table.upload_id, options: settings })}>按设置重新导入</Button>}
    </div></details>
    {!table ? <Empty description="CSV / XLSX / Parquet · 单文件最多 64 MB；表头行 0 表示无表头" /> : <>
      {table.warnings.length > 0 && <Alert type="warning" title="源文件提示" description={table.warnings.join('；')} />}
      <div className="data-step-controls">
        <Select aria-label={`${side}清洗操作`} value={operator} onChange={setOperator} options={Object.entries(operatorNames).filter(([key]) => key !== 'source').map(([value, label]) => ({ value, label }))} />
        {!['duplicates', 'group_summary'].includes(operator) ? <Select aria-label={`${side}清洗列`} value={column} onChange={setColumn} options={options(table.columns)} /> :
          <Select aria-label={`${side}重复识别列`} mode="multiple" value={duplicates} onChange={setDuplicates} options={options(table.columns)} placeholder="选择字段" />}
        {operator === 'cast' && <Select aria-label={`${side}目标类型`} value={target} onChange={setTarget} options={[{ value: 'decimal', label: '金额/十进制' }, { value: 'integer', label: '整数' }, { value: 'date', label: '日期 YYYY-MM-DD' }, { value: 'boolean', label: '真假值' }]} />}
        {operator === 'filter' && <Select aria-label={`${side}筛选条件`} value={predicate} onChange={setPredicate} options={[{ value: 'equals', label: '等于' }, { value: 'not_equals', label: '不等于' }, { value: 'is_null', label: '为空值' }, { value: 'not_null', label: '非空值' }]} />}
        {(operator === 'rename' || operator === 'map' || operator === 'filter' && !['is_null', 'not_null'].includes(predicate)) && <Input aria-label={`${side}操作值`} placeholder={operator === 'rename' ? '新列名' : operator === 'map' ? '原值' : '比较值'} value={text} onChange={event => setText(event.target.value)} />}
        {operator === 'map' && <Input aria-label={`${side}映射新值`} placeholder="新值" value={replacement} onChange={event => setReplacement(event.target.value)} />}
        {operator === 'duplicates' && <Checkbox checked={includeNull} onChange={event => setIncludeNull(event.target.checked)}>包含空值</Checkbox>}
        {operator === 'group_summary' && <><Select aria-label={`${side}金额汇总列`} mode="multiple" value={amountColumns} onChange={setAmountColumns} options={options(table.columns)} placeholder="金额列（可留空仅计数）" /><Text type="secondary">先转为金额类型；未知金额会报错，保留全部来源行。</Text></>}
        {operator === 'filter_compare' && <><Select aria-label={`${side}比较类型`} value={valueKind} onChange={setValueKind} options={[{ value: 'decimal', label: '十进制数' }, { value: 'date', label: '日期' }, { value: 'text', label: '文字' }]} />
          <Select aria-label={`${side}区间条件`} value={typedPredicate} onChange={setTypedPredicate} options={[{ value: 'gt', label: '大于' }, { value: 'ge', label: '大于等于' }, { value: 'lt', label: '小于' }, { value: 'le', label: '小于等于' }, { value: 'between', label: '闭区间' }]} /><Input aria-label={`${side}比较下限`} value={text} onChange={e => setText(e.target.value)} placeholder="比较值 / 下限" />{typedPredicate === 'between' && <Input aria-label={`${side}比较上限`} value={upper} onChange={e => setUpper(e.target.value)} placeholder="上限" />}</>}
        <Button type="primary" disabled={busy || !column} onClick={apply}>应用步骤</Button>
      </div>
      <Space className="data-history" wrap><Select aria-label={`${side}历史版本`} value={table.revision} style={{ minWidth: 235 }} disabled={busy}
        onChange={revision => void run('select_revision', { side, revision })} options={(table.versions || table.history).map((entry, index) => ({ value: entry.revision,
          label: `${index + 1}. ${operatorNames[entry.step?.operator || 'source']} · ${entry.output_rows} 行` }))} />
        <Button disabled={busy || table.history.length < 2} onClick={() => void run('undo', { side })}>撤销一步</Button>
        <Button disabled={busy} onClick={() => void run('export_table', { side })}>导出当前表</Button></Space>
      {preview.isError && <Alert type="error" title="预览加载失败" action={<Button onClick={() => void preview.refetch()}>重试</Button>} />}
      {preview.data && (preview.data.metadata.event.errors_total > 0 || preview.data.metadata.duplicate_groups > 0) && <Alert type="warning"
        title={`转换异常 ${preview.data.metadata.event.errors_total} 条 · 重复分组 ${preview.data.metadata.duplicate_groups} 组`}
        description={preview.data.metadata.event.error_sample.slice(0, 3).map(item => `${item.raw} (${item.code})`).join('；')} />}
      <Table<DataRow> size="small" className="data-preview" loading={preview.isPending} dataSource={preview.data?.rows || []} rowKey="row_id" scroll={{ x: 'max-content', y: 232 }}
        columns={[{ title: '原行', dataIndex: 'source_row', width: 65, fixed: 'left' }, ...table.columns.map((item, index) => ({ title: label(item), key: item.column_id, width: 150,
          render: (_: unknown, row: DataRow) => { const cell = row.cells[index]; return <span className={cell.kind === 'decimal' || cell.kind === 'integer' ? 'data-cell data-cell-number' : 'data-cell'} data-value-kind={cell.kind} title={`类型：${cell.kind}\n原值：${cell.raw ?? 'NULL'}${cell.warning ? '\n' + cell.warning : ''}`}>{cell.value === null ? <Text type="secondary">{cell.kind === 'unknown_formula' ? '公式结果未知' : 'NULL'}</Text> : cell.value === '' ? <Text type="secondary">空字符串</Text> : String(cell.value)}</span> } }))]}
        pagination={{ current: page, pageSize: 20, total: preview.data?.total || 0, showSizeChanger: false, onChange: setPage }} />
      <details className="data-options"><summary>清洗配方 <span>{session.state.recipes.length ? `已保存 ${session.state.recipes.length} 份` : '保存或复用清洗步骤'}</span></summary>
        <RecipeControls table={table} side={side} recipes={session.state.recipes} busy={busy} run={run} /></details>
    </>}
  </section>
}

type Pair = { left: string; right: string; mode: 'key' | 'compare' | 'ignore'; amount: boolean }
function ComparePanel({ session, busy, run }: { session: Session; busy: boolean; run: Run }) {
  const left = session.state.tables.left, right = session.state.tables.right
  const [pairs, setPairs] = useState<Pair[]>([])
  const [typePolicy, setTypePolicy] = useState('strict'), [nullPolicy, setNullPolicy] = useState('never_match')
  const [tolerance, setTolerance] = useState('0'), [trim, setTrim] = useState(false), [fold, setFold] = useState(false)
  const draftKey = left && right ? `data-compare:${session.id}:${left.revision}:${right.revision}` : null
  const [loadedKey, setLoadedKey] = useState<string | null>(null)
  useEffect(() => {
    if (!left || !right || !draftKey) return
    try {
      const raw = localStorage.getItem(draftKey)
      if (raw) {
        const saved = JSON.parse(raw)
        setPairs(saved.pairs); setTypePolicy(saved.typePolicy); setNullPolicy(saved.nullPolicy)
        setTolerance(saved.tolerance); setTrim(saved.trim); setFold(saved.fold); setLoadedKey(draftKey)
        return
      }
    } catch { /* A unavailable browser draft does not prevent a new comparison. */ }
    const previous = session.state.comparison
    const policy = previous?.left_revision === left.revision && previous?.right_revision === right.revision ? previous.spec : null
    setPairs(left.columns.map((column, index) => ({ left: column.column_id,
      right: right.columns.find(item => item.header === column.header)?.column_id || right.columns[index]?.column_id || '',
      mode: index === 0 ? 'key' : 'compare', amount: false })))
    if (policy) {
      setPairs(left.columns.map(column => {
        const key = policy.key_pairs.find(pair => pair[0] === column.column_id)
        const compared = policy.compare_pairs.find(pair => pair[0] === column.column_id)
        return { left: column.column_id, right: key?.[1] || compared?.[1] || '', mode: key ? 'key' : compared ? 'compare' : 'ignore',
          amount: policy.amount_pairs.some(pair => pair[0] === column.column_id) }
      }))
      setTypePolicy(policy.type_policy); setNullPolicy(policy.null_policy); setTolerance(policy.amount_tolerance)
      setTrim(policy.trim_text); setFold(policy.casefold_text)
    }
    setLoadedKey(draftKey)
  }, [draftKey])
  useEffect(() => {
    if (draftKey && loadedKey === draftKey) {
      try { localStorage.setItem(draftKey, JSON.stringify({ pairs, typePolicy, nullPolicy, tolerance, trim, fold })) } catch { /* Optional unsent draft. */ }
    }
  }, [draftKey, loadedKey, pairs, typePolicy, nullPolicy, tolerance, trim, fold])
  if (!left || !right) return <section id="data-compare" className="panel"><Title level={3}>比对设置</Title><Empty description="导入左右两份表格后设置匹配关系" /></section>
  const change = (index: number, value: Partial<Pair>) => setPairs(old => old.map((pair, i) => i === index ? { ...pair, ...value } : pair))
  const compare = () => void run('compare', { spec: {
    key_pairs: pairs.filter(pair => pair.mode === 'key').map(pair => [pair.left, pair.right]),
    compare_pairs: pairs.filter(pair => pair.mode === 'compare').map(pair => [pair.left, pair.right]),
    amount_pairs: pairs.filter(pair => pair.mode === 'compare' && pair.amount).map(pair => [pair.left, pair.right]),
    type_policy: typePolicy, null_policy: nullPolicy, amount_tolerance: tolerance, trim_text: trim, casefold_text: fold,
  } })
  return <section id="data-compare" className="panel"><div className="section-head"><Title level={3}>匹配键与比对列</Title>
    <Button type="primary" disabled={busy || !pairs.some(pair => pair.mode === 'key') || pairs.some(pair => pair.mode !== 'ignore' && !pair.right)} onClick={compare}>开始比对</Button></div>
    <Paragraph type="secondary">明确选择匹配键和需要比较的列。重复键会单独列出，不自动配对。</Paragraph>
    <div className="data-pairs">{pairs.map((pair, index) => <div className="data-pair" key={pair.left}><Text>{label(left.columns[index])}</Text><span>→</span>
      <Select aria-label={`右侧映射${pair.left}`} value={pair.right || undefined} onChange={right => change(index, { right })} options={options(right.columns)} />
      <Select aria-label={`列用途${pair.left}`} value={pair.mode} onChange={mode => change(index, { mode })} options={[{ value: 'key', label: '匹配键' }, { value: 'compare', label: '比对值' }, { value: 'ignore', label: '忽略' }]} />
      <Checkbox disabled={pair.mode !== 'compare'} checked={pair.amount} onChange={event => change(index, { amount: event.target.checked })}>金额</Checkbox></div>)}</div>
    <Space wrap className="data-policies"><Select aria-label="类型策略" value={typePolicy} onChange={setTypePolicy} options={[{ value: 'strict', label: '区分列类型' }, { value: 'text', label: '按文本比较' }]} />
      <Select aria-label="空值策略" value={nullPolicy} onChange={setNullPolicy} options={[{ value: 'never_match', label: '空值不匹配' }, { value: 'equal', label: '空值视为相同' }]} />
      <label>金额容差 <Input aria-label="金额容差" style={{ width: 100 }} value={tolerance} onChange={event => setTolerance(event.target.value)} /></label>
      <Checkbox checked={trim} onChange={event => setTrim(event.target.checked)}>比较时去首尾空白</Checkbox>
      <Checkbox checked={fold} onChange={event => setFold(event.target.checked)}>忽略大小写</Checkbox></Space>
  </section>
}

function Results({ session, busy, run, workspaceId }: { session: Session; busy: boolean; run: Run; workspaceId: string | null }) {
  const [category, setCategory] = useState(''), [page, setPage] = useState(1)
  const [selected, setSelected] = useState<string | null>(session.state.result_id)
  const [publishing, setPublishing] = useState<string | null>(null)
  const [publishError, setPublishError] = useState('')
  const [published, setPublished] = useState<Record<string, { workspace: string; bundle: string }>>({})
  const publish = async (exportId: string) => {
    if (!workspaceId) return
    setPublishing(exportId); setPublishError('')
    try {
      const response = await fetch(`/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/artifacts/data-exports`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: session.id, export_id: exportId }),
      })
      if (!response.ok) {
        const body = await response.json().catch(() => ({}))
        throw new Error(typeof body.detail === 'string' ? body.detail : `归集失败（${response.status}）`)
      }
      const bundle = await response.json() as { bundle_id: string }
      setPublished(old => ({ ...old, [exportId]: { workspace: workspaceId, bundle: bundle.bundle_id } }))
    } catch (failure) { setPublishError((failure as Error).message) }
    finally { setPublishing(null) }
  }
  const [sourceParams] = useSearchParams()
  const requestedResult = sourceParams.get('result')
  useEffect(() => { setSelected(requestedResult || session.state.result_id); setPage(1) }, [session.state.result_id, requestedResult])
  const query = useQuery({ queryKey: ['data-result', session.id, selected, category, page], enabled: !!selected,
    queryFn: () => request<ResultPage>(`/sessions/${session.id}/results/${selected}?offset=${(page - 1) * 30}&limit=30${category ? '&category=' + category : ''}`) })
  const metadata = query.data?.metadata
  return <section id="data-results" className="panel"><div className="section-head"><Title level={3}>差异结果</Title><Space wrap>
    <Select aria-label="历史比对" placeholder="选择一次比对" value={selected || undefined} style={{ minWidth: 220 }} onChange={value => { setSelected(value); setPage(1) }}
      options={session.state.comparisons.map((item, i) => ({ value: item.id, label: `${i + 1}. ${item.left_name} / ${item.right_name}` }))} />
    <Button type="primary" disabled={busy || !selected} onClick={() => void run('export', { result_id: selected })}>生成下载文件</Button>
    {workspaceId && selected && session.workspace_id === workspaceId && <Link to={`/agent?workspace=${encodeURIComponent(workspaceId)}&data_session=${encodeURIComponent(session.id)}&result=${encodeURIComponent(selected)}`}><Button>交给 Agent 继续处理</Button></Link>}</Space></div>
    {!selected ? <Empty description="完成比对后，这里显示固定版本的结果" /> : <>
      {metadata && <div className="data-counts">{[['相同', 'same_pairs'], ['差异', 'different_pairs'], ['仅左', 'only_left'], ['仅右', 'only_right'], ['重复键', 'ambiguous_keys']].map(([title, key]) =>
        <div key={key}><Text type="secondary">{title}</Text><strong>{metadata.coverage[key]}</strong></div>)}</div>}
      <Select aria-label="结果分类" value={category} style={{ width: 160, marginBottom: 16 }} onChange={value => { setCategory(value); setPage(1) }} options={[{ value: '', label: '全部结果' }, ...Object.entries(categoryNames).map(([value, label]) => ({ value, label }))]} />
      {query.isError && <Alert type="error" title="结果加载失败" action={<Button onClick={() => void query.refetch()}>重试</Button>} />}
      <Table size="small" loading={query.isPending} dataSource={query.data?.rows || []} rowKey="ordinal" scroll={{ x: 'max-content', y: 360 }}
        columns={[{ title: '分类', dataIndex: 'category', width: 100, fixed: 'left', render: value => <Tag color={value === 'different' ? 'orange' : value === 'same' ? 'green' : 'default'}>{categoryNames[value] || value}</Tag> },
          { title: '左原行', dataIndex: 'left_source_row', width: 75 }, { title: '右原行', dataIndex: 'right_source_row', width: 75 },
          ...(metadata ? (['left', 'right'] as const).flatMap(side => metadata[side].columns.map(column => ({ title: `${side === 'left' ? '左' : '右'} · ${label(column)}`,
            dataIndex: `${side === 'left' ? 'L' : 'R'}_${column.column_id}_value`, width: 145 }))) : [])]}
        pagination={{ current: page, pageSize: 30, total: query.data?.total || 0, showSizeChanger: false, onChange: setPage }} />
    </>}
    {session.state.exports.length > 0 && <div className="data-downloads"><Title level={4}>已生成的固定版本文件</Title>
      {publishError && <Alert type="error" title={publishError} closable onClose={() => setPublishError('')} />}
      {session.state.exports.map((item, index) => <div className="data-download-row" key={item.id}><Text type="secondary">第 {session.state.exports.length - index} 次导出</Text>
        <Space wrap>{item.files.map(file => <a key={file.name} href={`${API}/sessions/${session.id}/exports/${item.id}/${file.name}`} download>{file.name} <small>({Math.ceil(file.bytes / 1024)} KB)</small></a>)}</Space>
        {workspaceId && (session.workspace_id === workspaceId
          ? <Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}`}>查看项目输出</Link>
          : published[item.id]?.workspace === workspaceId
          ? <Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}&bundle=${encodeURIComponent(published[item.id].bundle)}`}>查看成果</Link>
          : <Button size="small" loading={publishing === item.id} disabled={publishing !== null}
              onClick={() => void publish(item.id)}>归集到成果中心</Button>)}</div>)}
      <Text type="secondary">CSV 对可能被表格软件执行的文本添加安全前缀；原始值和左右来源保存在配套清单中。</Text></div>}
  </section>
}

export function DataWorkbench() {
  const [params, setParams] = useSearchParams()
  const sessionId = params.get('session')
  const workspaceId = params.get('workspace')
  const [historyPage, setHistoryPage] = useState({ workspaceId, page: 1 })
  const sessionPage = historyPage.workspaceId === workspaceId ? historyPage.page : 1
  const projects = useWorkspaces()
  const projectName = projects.data?.find(item => item.workspace_id === workspaceId)?.name
  const assetSetId = params.get('asset_set')
  const client = useQueryClient()
  const [name, setName] = useState('新建表格比对'), [error, setError] = useState(''), [sending, setSending] = useState(false)
  const [leftItem, setLeftItem] = useState<string>(), [rightItem, setRightItem] = useState<string>()
  const assetSet = useQuery({ queryKey: ['data-asset-set', workspaceId, assetSetId], enabled: !!workspaceId && !!assetSetId,
    queryFn: async ({ signal }) => {
      const response = await fetch(`/api/platform/v1/workspaces/${encodeURIComponent(workspaceId || '')}/assets/sets/${encodeURIComponent(assetSetId || '')}`, { signal })
      if (!response.ok) throw new Error(`资料集读取失败（${response.status}）`)
      return response.json() as Promise<AssetSelection>
    } })
  const tableItems = assetSet.data?.items.flatMap((item, index) => {
    const member = assetSet.data.members[index]
    return member && /\.(csv|xlsx|parquet)$/i.test(member.original_name)
      ? [{ value: item.item_id, label: member.original_name }] : []
  }) || []
  useEffect(() => { setLeftItem(tableItems[0]?.value); setRightItem(tableItems[1]?.value) }, [assetSet.data?.asset_set_id])
  const health = useQuery({ queryKey: ['data-health'], queryFn: () => request<{ ready: boolean; message: string | null }>('/health'), refetchInterval: 15_000 })
  const list = useQuery({ queryKey: ['data-sessions', workspaceId, sessionPage], queryFn: () => request<{ items: { id: string; name: string }[]; total: number }>(`/sessions?${workspaceId ? `workspace_id=${encodeURIComponent(workspaceId)}&` : ''}offset=${(sessionPage - 1) * 20}&limit=20`) })
  const session = useQuery({ queryKey: ['data-session', sessionId], enabled: !!sessionId,
    queryFn: () => request<Session>(`/sessions/${sessionId}`),
    refetchInterval: query => query.state.data?.jobs.some(job => active.includes(job.status)) ? 750 : false })
  const wrongProject = !!session.data?.workspace_id && session.data.workspace_id !== workspaceId
  const current = wrongProject ? undefined : session.data
  const job = current?.jobs.find(item => active.includes(item.status))
  const busy = sending || !!job || !health.data?.ready
  const refresh = async () => { await client.invalidateQueries({ queryKey: ['data-session', sessionId] }); void client.invalidateQueries({ queryKey: ['data-sessions'] }) }
  const choose = (id: string) => { const next = new URLSearchParams(params); next.set('session', id); setParams(next); setError('') }
  const create = async () => {
    setSending(true); setError('')
    const key = `clear-data-create:${workspaceId || 'standalone'}`
    try {
      const pending = localStorage.getItem(key)
      const payload = pending ? JSON.parse(pending) : { name: projectName ? `${projectName.slice(0, 85)} · 表格比对` : name,
        workspace_id: workspaceId, request_id: `s${crypto.randomUUID().replaceAll('-', '')}` }
      localStorage.setItem(key, JSON.stringify(payload))
      const result = await post<Session>('/sessions', payload)
      localStorage.removeItem(key)
      client.setQueryData(['data-session', result.id], result)
      await client.invalidateQueries({ queryKey: ['data-sessions'] }); choose(result.id)
    }
    catch (failure) { setError((failure as Error).message) } finally { setSending(false) }
  }
  const importSelection = async () => {
    if (!workspaceId || !assetSet.data || !leftItem || !rightItem || leftItem === rightItem) return
    setSending(true); setError('')
    try {
      let work = await post<Session>('/sessions', { name: '资料库表格比对', workspace_id: workspaceId })
      choose(work.id)
      for (const [side, itemId] of ([['left', leftItem], ['right', rightItem]] as const)) {
        const upload = await post<{ id: string }>(`/sessions/${work.id}/asset-uploads`, {
          workspace_id: workspaceId, asset_set_id: assetSet.data.asset_set_id,
          asset_set_revision: assetSet.data.revision, item_id: itemId,
        })
        const job = await post<Job>(`/sessions/${work.id}/actions`, {
          action: 'import', version: work.version,
          payload: { side, upload_id: upload.id, options: { header_row: 1 } },
        })
        let finished = false
        for (let attempt = 0; attempt < 450; attempt++) {
          const status = await request<Job>(`/sessions/${work.id}/jobs/${job.id}`)
          if (!active.includes(status.status)) {
            if (status.status !== 'succeeded') throw new Error(status.error || '资料导入未完成')
            finished = true
            break
          }
          await new Promise(resolve => window.setTimeout(resolve, 200))
        }
        if (!finished) throw new Error('资料导入等待超时，请在当前作业检查状态')
        work = await request<Session>(`/sessions/${work.id}`)
      }
      await client.invalidateQueries({ queryKey: ['data-session', work.id] })
      void client.invalidateQueries({ queryKey: ['data-sessions'] })
    } catch (failure) { setError((failure as Error).message) }
    finally { setSending(false) }
  }
  const run: Run = async (action, payload) => {
    if (!current) return
    setSending(true); setError('')
    try {
      const result = await post<Job>(`/sessions/${current.id}/actions`, { action, payload, version: current.version })
      client.setQueryData<Session>(['data-session', current.id], old => old ? { ...old, jobs: [result, ...old.jobs] } : old)
      await refresh()
    } catch (failure) { setError((failure as Error).message); await refresh() } finally { setSending(false) }
  }
  const upload = async (side: Side, file: File, settings: ImportOptions) => {
    if (!current) return
    setSending(true); setError('')
    try {
      const source = await request<{ id: string }>(`/sessions/${current.id}/uploads?name=${encodeURIComponent(file.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file })
      const result = await post<Job>(`/sessions/${current.id}/actions`, { action: 'import', version: current.version, payload: { side, upload_id: source.id, options: { ...settings, sheet_name: undefined } } })
      client.setQueryData<Session>(['data-session', current.id], old => old ? { ...old, jobs: [result, ...old.jobs] } : old)
      await refresh()
    } catch (failure) { setError((failure as Error).message); await refresh() } finally { setSending(false) }
  }
  return <div className="data-workbench"><div className="page-heading"><div className="data-title-line"><Title level={1}>{brands.data}</Title>
    <Tag color={health.isPending ? 'processing' : health.data?.ready ? 'success' : 'warning'}>{health.isPending ? '连接中' : health.data?.ready ? '服务就绪' : '服务不可用'}</Tag></div>
    <Paragraph>导入两份表格，按需要清洗和比对，直接生成下载文件。</Paragraph></div>
    {(!health.data?.ready || health.isError) && !health.isPending && <Alert type="warning" title={health.data?.message || '无法连接数据服务'} action={<Button onClick={() => void health.refetch()}>重试</Button>} />}
    <div className="data-session-bar"><Select aria-label="数据作业" placeholder="继续已有作业" value={sessionId || undefined} onChange={choose} loading={list.isPending} options={[
      ...(current && !list.data?.items.some(item => item.id === current.id) ? [{ value: current.id, label: current.name }] : []),
      ...(list.data?.items.map(item => ({ value: item.id, label: item.name })) || [])]} />
      <Pagination aria-label="作业历史分页" size="small" current={sessionPage} pageSize={20} total={list.data?.total || 0} showSizeChanger={false}
        hideOnSinglePage onChange={page => setHistoryPage({ workspaceId, page })} />
      {list.isError && <Alert type="error" title="作业历史读取失败" action={<Button onClick={() => void list.refetch()}>重试</Button>} />}
      <Space.Compact>{!workspaceId && <Input aria-label="新作业名称" value={name} maxLength={100} onChange={event => setName(event.target.value)} />}<Button disabled={sending || !health.data?.ready || !!workspaceId && !projectName} onClick={() => void create()}>{workspaceId ? '开始新的比对' : '新建作业'}</Button></Space.Compact>
      {workspaceId && <Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}`}>项目输出</Link>}
      {current && <div className="data-session-context"><Text strong>{current.name}</Text><Text type="secondary">作业版本 {current.version}</Text>
        {current.jobs[0] && <Tag aria-live="polite" color={job ? 'processing' : current.jobs[0].status === 'failed' ? 'error' : current.jobs[0].status === 'succeeded' ? 'success' : 'default'}>
          {jobNames[(job || current.jobs[0]).action] || '操作'}{jobStatusNames[(job || current.jobs[0]).status] || (job || current.jobs[0]).status}</Tag>}
        <nav className="data-flow-nav" aria-label="数据工作步骤"><a href="#data-inputs">输入与清洗</a><a href="#data-compare">比对设置</a><a href="#data-results">结果与导出</a></nav></div>}</div>
    {workspaceId && assetSetId && <section className="panel data-asset-selection"><div className="section-head"><Title level={3}>从资料库导入</Title>
      <Link to={`/assets?workspace=${encodeURIComponent(workspaceId)}&set=${encodeURIComponent(assetSetId)}`}>返回资料库</Link></div>
      {assetSet.isError && <Alert type="error" title={assetSet.error.message} action={<Button onClick={() => void assetSet.refetch()}>重试</Button>} />}
      <Space wrap><Select aria-label="左侧资料" placeholder="左侧资料" value={leftItem} onChange={setLeftItem} style={{ minWidth: 180 }} options={tableItems} />
        <Select aria-label="右侧资料" placeholder="右侧资料" value={rightItem} onChange={setRightItem} style={{ minWidth: 180 }} options={tableItems} />
        <Button type="primary" loading={sending} disabled={!health.data?.ready || !leftItem || !rightItem || leftItem === rightItem}
          onClick={() => void importSelection()}>新建作业并导入</Button></Space>
    </section>}
    {error && <Alert type="error" title={error} closable onClose={() => setError('')} />}
    {session.isError && <Alert type="error" title="无法读取作业" action={<Button onClick={() => void session.refetch()}>重试</Button>} />}
    {wrongProject && <Alert type="warning" title="这个数据作业属于其他项目，请选择当前项目的作业" />}
    {job && <Alert role="status" aria-live="polite" type="info" showIcon icon={<Spin size="small" />} title={`${jobNames[job.action] || '操作'}${job.status === 'queued' ? '等待执行' : job.status === 'cancelling' ? '正在取消' : '进行中'}`}
      action={<Button disabled={job.status === 'cancelling'} onClick={() => void post(`/sessions/${current!.id}/jobs/${job.id}/cancel`, {}).then(refresh).catch(failure => setError(failure.message))}>取消</Button>} />}
    {!job && current?.jobs[0]?.error && <Alert type="warning" title={current.jobs[0].error} />}
    {current ? <><FixedTable session={current} /><div id="data-inputs" className="data-input-grid"><InputPanel key={current.id + '-left'} session={current} side="left" busy={busy} run={run} upload={upload} />
      <InputPanel key={current.id + '-right'} session={current} side="right" busy={busy} run={run} upload={upload} /></div>
      <ComparePanel key={current.id} session={current} busy={busy} run={run} /><Results key={current.id + '-results'} session={current} busy={busy} run={run} workspaceId={workspaceId} /></> :
      <div className="empty-panel"><Empty description={session.isPending && sessionId ? '正在读取作业…' : '点击开始新的比对，或继续已有作业。'} /></div>}
  </div>
}
