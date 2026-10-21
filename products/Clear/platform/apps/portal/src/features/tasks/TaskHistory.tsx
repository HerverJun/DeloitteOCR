import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Checkbox, Descriptions, Drawer, Empty, Form, Input, Popconfirm, Select, Space, Table, Tabs, Tag, Timeline, Typography } from 'antd'
import type { TableColumnsType } from 'antd'
import { useSearchParams } from 'react-router-dom'
import type { TaskDetail, TaskPage, TaskSummary, TaskRun, TaskStepRun, TaskEvent } from '../../../../../packages/contracts/types.generated'
import { taskJson, postJson, useSession } from './session'
import './tasks.css'

const { Title, Text, Paragraph } = Typography
const labels: Record<string, string> = { queued: '排队中', waiting_resource: '等待资源', running: '运行中', waiting_user: '等待用户', pausing: '暂停中', paused: '已暂停', cancelling: '取消中', cancelled: '已取消', succeeded: '成功', partial: '部分成功', failed: '失败', needs_verification: '待核实' }
const tools: Record<string, string> = { ocr: 'ClearOCR', visual_review: '视觉审校', classification: 'ClearClassify', data: 'ClearData', forensics: 'ClearForensics', face: '人脸比对', audio: '音频分析', seal: '印章分析', catnet: '深度取证', document_forensics: '文档篡改排查', batch: '批次', agent: '智能审计', assets: '资料导入', artifact: '成果导出' }
const actions: Record<string, string> = { cancel: '取消', pause: '暂停', resume: '继续', retry: '重试', verify: '核实原执行', archive: '归档', unarchive: '取消归档' }
const eventLabels: Record<string, string> = { submitted: '任务已登记', native_snapshot: '执行端状态更新', control_requested: '控制操作已登记', control_delivered: '控制操作已发送', control_failed: '控制操作待核实', metadata_changed: '名称或说明已修改', verification_completed: '原执行核实完成', legacy_imported: '历史记录已迁入', child_bound: '子任务已关联', steering_requested: '追加消息已登记', steering_delivered: '追加消息已发送', observed: '执行状态已核实', lease_expired: '执行租约已过期' }
export const formatTaskSize = (bytes: number | null) => bytes === null ? '未记录' : bytes < 1024 ? `${bytes} B` : bytes < 1024 ** 2 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`
export const formatTaskDuration = (ms: number | null) => ms === null ? '未记录' : ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} 秒` : `${Math.floor(ms / 60000)} 分 ${Math.floor(ms % 60000 / 1000)} 秒`
const time = (value: string | null) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Hong_Kong' }) : '未记录'
const state = (value: string) => <Tag color={value === 'succeeded' ? 'success' : value === 'failed' ? 'error' : value === 'needs_verification' || value === 'partial' ? 'warning' : value === 'running' ? 'processing' : 'default'}>{labels[value] || value}</Tag>

function TaskInspector({ id, close, inspect }: { id: string | null; close: () => void; inspect: (id: string) => void }) {
  const client = useQueryClient()
  const [notice, setNotice] = useState('')
  const [events, setEvents] = useState<TaskEvent[]>([])
  const [liveEvents, setLiveEvents] = useState(true)
  const [eventCursors, setEventCursors] = useState<number[]>([0])
  const [eventPage, setEventPage] = useState(0)
  const historicalEvents = useQuery({ queryKey: ['task-events', id, eventCursors[eventPage]], enabled: !!id && !liveEvents, queryFn: () => taskJson<{ items: TaskEvent[]; next_cursor: number }>(`/api/platform/v2/tasks/${id}/events?after=${eventCursors[eventPage]}&limit=100`) })
  const shownEvents = liveEvents ? events : historicalEvents.data?.items || []
  const [editing, setEditing] = useState(false)
  const [extraRows, setExtraRows] = useState<Record<string, Record<string, unknown>[]>>({})
  const [extraCursors, setExtraCursors] = useState<Record<string, string | null>>({})
  const detail = useQuery({ queryKey: ['task-detail', id], enabled: !!id, queryFn: () => taskJson<TaskDetail>(`/api/platform/v2/tasks/${id}`), refetchInterval: 10000 })
  const task = detail.data?.task
  useEffect(() => {
    setEvents([]); setNotice(''); setEditing(false); setExtraRows({}); setExtraCursors({}); setLiveEvents(true); setEventCursors([0]); setEventPage(0)
    if (!id) return
    let mounted = true
    let refreshTimer: ReturnType<typeof setTimeout> | undefined
    const source = new EventSource(`/api/platform/v2/tasks/${id}/events`, { withCredentials: true })
    source.addEventListener('task', event => {
      const item = JSON.parse((event as MessageEvent).data) as TaskEvent
      if (mounted) setEvents(previous => previous.some(row => row.seq === item.seq) ? previous : [...previous, item].slice(-200))
      if (!refreshTimer) refreshTimer = setTimeout(() => { refreshTimer = undefined; if (!mounted) return; void client.invalidateQueries({ queryKey: ['task-detail', id] }); void client.invalidateQueries({ queryKey: ['task-history'] }) }, 500)
    })
    return () => { mounted = false; source.close(); if (refreshTimer) clearTimeout(refreshTimer) }
  }, [id, client])
  const control = useMutation({ mutationFn: (operation: string) => taskJson(`/api/platform/v2/tasks/${id}/controls`, postJson({ operation, expected_revision: task!.revision, idempotency_key: crypto.randomUUID() })),
    onSuccess: () => { setNotice('操作已记录，等待执行端确认。'); void client.invalidateQueries({ queryKey: ['task-detail', id] }); void client.invalidateQueries({ queryKey: ['task-history'] }) },
    onError: failure => setNotice(failure.message) })
  const edit = useMutation({ mutationFn: (value: { name: string; description: string }) => taskJson(`/api/platform/v2/tasks/${id}`, { ...postJson({ ...value, expected_revision: task!.revision }), method: 'PATCH' }),
    onSuccess: () => { setEditing(false); void client.invalidateQueries({ queryKey: ['task-detail', id] }); void client.invalidateQueries({ queryKey: ['task-history'] }) }, onError: failure => setNotice(failure.message) })
  const more = useMutation({ mutationFn: async (name: string) => {
    const cursor = name in extraCursors ? extraCursors[name] : detail.data?.collection_cursors?.[name]
    return { name, page: await taskJson<{ items: Record<string, unknown>[]; next_cursor: string | null }>(`/api/platform/v2/tasks/${id}/collections/${name}?limit=1000&cursor=${encodeURIComponent(cursor || '')}`) }
  }, onSuccess: ({ name, page }) => { setExtraRows(previous => ({ ...previous, [name]: [...(previous[name] || []), ...page.items] })); setExtraCursors(previous => ({ ...previous, [name]: page.next_cursor })) }, onError: error => setNotice(error.message) })
  const moreButtons = Object.entries(detail.data?.collection_cursors || {}).filter(([name, cursor]) => (name in extraCursors ? extraCursors[name] : cursor)).map(([name]) => <Button key={name} loading={more.isPending} onClick={() => more.mutate(name)}>继续加载{({ inputs: '输入', runs: '执行轮次', steps: '步骤', ai_calls: 'AI 调用', artifacts: '成果', children: '子任务' })[name as 'inputs']}</Button>)
  const full = detail.data ? Object.fromEntries(['inputs','runs','steps','ai_calls','artifacts','children'].map(name => [name, [...new Map([...(detail.data![name as 'inputs'] as Record<string, unknown>[]), ...(extraRows[name] || [])].map(row => [row.id, row])).values()]])) : {}
  const runColumns: TableColumnsType<TaskRun> = [
    { title: '轮次', dataIndex: 'attempt', width: 65 }, { title: '状态', dataIndex: 'status', render: state },
    { title: '执行发起人', render: (_, row) => String((row.request_snapshot.requested_by_snapshot as { display_name?: string } | undefined)?.display_name || '未记录') },
    { title: '入队', dataIndex: 'queued_at', render: time }, { title: '开始', dataIndex: 'started_at', render: time },
    { title: '结束', dataIndex: 'finished_at', render: time }, { title: '总耗时', dataIndex: 'elapsed_ms', render: formatTaskDuration },
    { title: '排队耗时', dataIndex: 'queue_ms', render: formatTaskDuration },
    { title: '错误', dataIndex: 'error', render: value => value ? <Paragraph ellipsis={{ rows: 2, expandable: true, symbol: '展开' }}>{value}</Paragraph> : '—' },
  ]
  const stepColumns: TableColumnsType<TaskStepRun> = [
    { title: '工具 / 操作', render: (_, row) => `${tools[row.tool_id] || row.tool_id} / ${row.action}` },
    { title: '执行编号', dataIndex: 'native_id', ellipsis: true }, { title: '尝试', dataIndex: 'attempt' },
    { title: '状态', dataIndex: 'status', render: state }, { title: '耗时', dataIndex: 'elapsed_ms', render: formatTaskDuration }, { title: '错误', dataIndex: 'error' },
  ]
  return <Drawer open={!!id} onClose={close} size="large" title={task?.name || '任务详情'} extra={task?.parent_task_id ? <Button onClick={() => inspect(task.parent_task_id!)}>返回主任务</Button> : undefined} className="task-inspector">
    {detail.isPending ? <Table loading columns={runColumns} dataSource={[]} /> : detail.isError ? <Alert type="error" title={detail.error.message} action={<Button onClick={() => void detail.refetch()}>重试</Button>} /> : task && detail.data && <>
      <Space wrap className="task-detail-heading"><Text code>{task.task_code}</Text>{state(task.status)}<Text type="secondary">{task.creator_name_snapshot || '未记录'} · {task.project_name_snapshot || '未归属项目'}</Text></Space>
      <Descriptions size="small" column={2} items={[
        { key: 'created', label: '创建时间', children: time(task.created_at) }, { key: 'size', label: '输入大小', children: formatTaskSize(task.input_total_bytes) },
        { key: 'elapsed', label: '最近执行总耗时（含等待）', children: formatTaskDuration(task.elapsed_ms) }, { key: 'source', label: '任务来源', children: task.source },
        { key: 'verified', label: '最后核实时间', children: time(task.last_verified_at) }, { key: 'stage', label: '执行阶段', children: task.stage || '—' },
        { key: 'note', label: '任务说明', children: task.description ? <Paragraph ellipsis={{ rows: 3, expandable: true, symbol: '展开' }}>{task.description}</Paragraph> : '未填写', span: 2 },
      ]} />
      <Space wrap className="task-controls">{task.capabilities.filter(action => actions[action]).map(action => <Popconfirm key={action} title={`确认${actions[action]}此任务？`} onConfirm={() => control.mutate(action)}>
        <Button loading={control.isPending} danger={action === 'cancel'}>{actions[action]}</Button></Popconfirm>)}
        {task.capabilities.includes('edit') && <Button onClick={() => setEditing(!editing)}>编辑名称与说明</Button>}</Space>
      {!!moreButtons.length && <Space wrap>{moreButtons}</Space>}
      {notice && <Alert type={control.isError || edit.isError ? 'error' : 'info'} title={notice} showIcon className="task-notice" />}
      {editing && <Form layout="vertical" initialValues={{ name: task.name, description: task.description }} onFinish={value => edit.mutate(value)}>
        <Form.Item name="name" label="任务名称" rules={[{ required: true }]}><Input maxLength={200} /></Form.Item>
        <Form.Item name="description" label="任务说明"><Input.TextArea rows={3} maxLength={10000} /></Form.Item><Button htmlType="submit" loading={edit.isPending}>保存</Button>
      </Form>}
      <Tabs items={[
        { key: 'runs', label: `执行记录 · ${task.run_count}`, children: <Table rowKey="id" size="small" columns={runColumns} dataSource={full.runs as unknown as TaskRun[]} scroll={{ x: 920 }} pagination={{ pageSize: 20 }} /> },
        { key: 'steps', label: `文件与步骤 · ${detail.data.steps.length}`, children: <Table rowKey="id" size="small" columns={stepColumns} dataSource={full.steps as unknown as TaskStepRun[]} scroll={{ x: 780 }} pagination={{ pageSize: 50 }} /> },
        { key: 'children', label: `子任务 · ${detail.data.children.length}`, children: <Table rowKey="id" size="small" dataSource={full.children} columns={[{ title: '子任务名称', dataIndex: 'name', render: (value, row) => <Button type="link" onClick={() => inspect(String(row.id))}>{String(value)}</Button> }, { title: '状态', dataIndex: 'status', render: value => state(String(value)) }]} /> },
        { key: 'inputs', label: '输入资料', children: <Table rowKey="id" size="small" dataSource={full.inputs} columns={[{ title: '文件', dataIndex: 'name' }, { title: '版本', dataIndex: 'asset_revision' }, { title: '大小', dataIndex: 'size_bytes', render: formatTaskSize }, { title: 'SHA256', dataIndex: 'blob_hash', ellipsis: true }]} /> },
        { key: 'ai', label: 'AI 调用', children: detail.data.ai_calls.length ? <Table rowKey="id" size="small" dataSource={full.ai_calls} columns={[{ title: '连接', dataIndex: 'connection_name', render: value => value || '未记录' }, { title: '模型', dataIndex: 'actual_model', render: value => value || '未记录' }, { title: '输入 token', dataIndex: 'input_tokens' }, { title: '输出 token', dataIndex: 'output_tokens' }, { title: '耗时', dataIndex: 'elapsed_ms', render: formatTaskDuration }]} /> : <Empty description="没有已记录的 AI 调用" /> },
        { key: 'artifacts', label: '成果', children: <Table rowKey="id" size="small" dataSource={full.artifacts} columns={[{ title: '成果名称', dataIndex: 'name' }, { title: '大小', dataIndex: 'size_bytes', render: formatTaskSize }, { title: '操作', render: (_, row) => row.missing ? '文件已清理' : <a href={`/api/platform/v2/tasks/${id}/artifacts/${row.id}/download`}>下载</a> }]} /> },
        { key: 'events', label: '事件历史', children: <><Space className="task-controls"><Button onClick={() => setLiveEvents(true)}>最近 200 条事件</Button><Button onClick={() => { setLiveEvents(false); setEventCursors([0]); setEventPage(0) }}>从开始查看完整历史</Button></Space>
          {historicalEvents.isError && !liveEvents && <Alert type="error" title={historicalEvents.error.message} />}
          {shownEvents.length ? <Timeline items={shownEvents.map(item => { const snapshot = item.payload.snapshot as Record<string, unknown> | undefined; const status = String(item.payload.status || snapshot?.status || snapshot?.state || ''); return { key: item.seq, title: time(item.created_at), content: <><Text>{eventLabels[item.kind] || item.kind}</Text><Paragraph type="secondary">{labels[status] || actions[String(item.payload.action)] || String(item.payload.stage || snapshot?.phase || '')}</Paragraph></> } })} /> : <Empty description={historicalEvents.isFetching ? '正在读取事件' : '当前页没有事件'} />}
          {!liveEvents && <Space><Button disabled={!eventPage} onClick={() => setEventPage(eventPage - 1)}>上一页</Button><Text>第 {eventPage + 1} 页 · 每页 100 条</Text><Button disabled={historicalEvents.data?.items.length !== 100} onClick={() => { setEventCursors(previous => [...previous.slice(0, eventPage + 1), historicalEvents.data!.next_cursor]); setEventPage(eventPage + 1) }}>下一页</Button></Space>}</> },
      ]} />
    </>}
  </Drawer>
}

export function TaskHistory() {
  const [params] = useSearchParams(); const workspace = params.get('workspace') || ''
  const { user } = useSession()
  const [keyword, setKeyword] = useState(''); const [tool, setTool] = useState(''); const [status, setStatus] = useState(''); const [source, setSource] = useState('')
  const [mine, setMine] = useState(false); const [archived, setArchived] = useState(false); const [from, setFrom] = useState(''); const [to, setTo] = useState('')
  const [service, setService] = useState('')
  const [submitter, setSubmitter] = useState('')
  const submitters = useQuery({ queryKey: ['task-visible-users'], queryFn: () => taskJson<{ items: { id: string; display_name: string }[] }>('/api/platform/v2/visible-users') })
  const services = useQuery({ queryKey: ['task-ai-services'], queryFn: () => taskJson<{ items: { id: string; name: string }[] }>('/api/platform/v2/ai-services') })
  const [cursors, setCursors] = useState<(string | null)[]>([null]); const [page, setPage] = useState(0); const [selected, setSelected] = useState<string | null>(null)
  const [extra, setExtra] = useState<string[]>(['description'])
  useEffect(() => { setCursors([null]); setPage(0) }, [workspace, keyword, tool, status, source, mine, archived, from, to, service, submitter])
  const filters = new URLSearchParams({ limit: '50' })
  if (workspace) filters.set('workspace_id', workspace)
  if (keyword) filters.set('keyword', keyword)
  if (tool) filters.set('tool_id', tool)
  if (status) filters.set('status', status)
  if (source) filters.set('source', source)
  if (service) filters.set('ai_service', service)
  if (mine && user) filters.set('created_by', user.id)
  else if (submitter) filters.set('created_by', submitter)
  if (archived) filters.set('archived', 'true')
  if (from) filters.set('created_from', new Date(from + '+08:00').toISOString())
  if (to) filters.set('created_to', new Date(to + '+08:00').toISOString())
  if (cursors[page]) filters.set('cursor', cursors[page]!)
  const list = useQuery({ queryKey: ['task-history', filters.toString()], queryFn: ({ signal }) => taskJson<TaskPage>(`/api/platform/v2/tasks?${filters}`, { signal }), refetchInterval: 15000 })
  const columns: TableColumnsType<TaskSummary> = [
    { title: '序号', key: 'ordinal', width: 65, fixed: 'left', render: (_, __, index) => page * 50 + index + 1 },
    { title: '操作', key: 'actions', width: 75, fixed: 'left', render: (_, row) => <Button size="small" type="link" onClick={() => setSelected(row.id)}>详情</Button> },
    { title: '任务编码', dataIndex: 'task_code', width: 225, render: value => <Text className="task-number">{value}</Text> },
    { title: '任务名称', dataIndex: 'name', width: 220, ellipsis: true, render: (value, row) => <Button type="link" className="task-name-link" onClick={() => setSelected(row.id)}>{value}</Button> },
    { title: '工具名称', dataIndex: 'tool_id', width: 125, render: value => tools[value] || value },
    { title: 'AI 服务', width: 170, render: (_, row) => Array.isArray(row.summary.ai_services) && row.summary.ai_services.length ? row.summary.ai_services.join('、') : ['import','export'].includes(row.action) ? '不适用' : ['agent', 'visual_review', 'ocr', 'classification', 'face', 'audio', 'seal', 'catnet', 'document_forensics'].includes(row.tool_id) ? '未记录' : '不适用' },
    { title: '任务状态', dataIndex: 'status', width: 120, render: state },
    { title: '项目名称', dataIndex: 'project_name_snapshot', width: 180, ellipsis: true, render: value => value || '未归属项目' },
    { title: '提交人', dataIndex: 'creator_name_snapshot', width: 125, render: value => value || '未记录' },
    { title: '输入大小', dataIndex: 'input_total_bytes', width: 110, render: formatTaskSize },
    { title: '创建时间', dataIndex: 'created_at', width: 190, render: time },
    { title: '最近执行耗时', dataIndex: 'elapsed_ms', width: 135, render: formatTaskDuration },
    ...(extra.includes('description') ? [{ title: '任务说明', dataIndex: 'description', width: 240, ellipsis: true }] : []),
    ...(extra.includes('source') ? [{ title: '来源', dataIndex: 'source', width: 100 }] : []),
    ...(extra.includes('attempts') ? [{ title: '执行次数', dataIndex: 'run_count', width: 95 }] : []),
  ]
  return <section className="task-history"><div className="page-heading"><Text className="eyebrow">TASK HISTORY</Text><Title level={1}>任务中心</Title><Paragraph type="secondary">查看执行进度、历次尝试和成果。{workspace ? '当前项目任务' : '你有权限访问的全部项目任务'}。</Paragraph></div>
    <div className="task-filters"><Input.Search aria-label="搜索任务" placeholder="任务名称或编码" allowClear onSearch={setKeyword} />
      <Select aria-label="筛选工具" placeholder="全部工具" allowClear onChange={value => setTool(value || '')} options={Object.entries(tools).map(([value, label]) => ({ value, label }))} />
      <Select aria-label="筛选状态" placeholder="全部状态" allowClear onChange={value => setStatus(value || '')} options={Object.entries(labels).map(([value, label]) => ({ value, label }))} />
      <Select aria-label="筛选来源" placeholder="全部来源" allowClear onChange={value => setSource(value || '')} options={[{ value: 'manual', label: '手动工具' }, { value: 'agent', label: '智能审计' }, { value: 'batch', label: '自动批次' }, { value: 'legacy', label: '历史迁移' }]} />
      <Select aria-label="筛选 AI 服务" placeholder="全部 AI 服务" allowClear onChange={value => setService(value || '')} options={[{ value: 'local', label: '本地模型' }, ...(services.data?.items || []).map(row => ({ value: row.id, label: row.name }))]} />
      <Select aria-label="筛选提交人" placeholder="全部提交人" allowClear disabled={mine} onChange={value => setSubmitter(value || '')} options={submitters.data?.items.map(row => ({ value: row.id, label: row.display_name }))} />
      <label>创建起始<input aria-label="创建起始时间" type="datetime-local" value={from} onChange={event => setFrom(event.target.value)} /></label>
      <label>创建截止<input aria-label="创建截止时间" type="datetime-local" value={to} onChange={event => setTo(event.target.value)} /></label>
    </div>
    <div className="task-table-toolbar"><Space wrap><Checkbox checked={mine} onChange={event => setMine(event.target.checked)}>仅我提交</Checkbox><Checkbox checked={archived} onChange={event => setArchived(event.target.checked)}>查看归档</Checkbox></Space>
      <Space><Select mode="multiple" aria-label="显示附加列" value={extra} onChange={setExtra} options={[{ value: 'description', label: '任务说明' }, { value: 'source', label: '来源' }, { value: 'attempts', label: '执行次数' }]} /><Button onClick={() => void list.refetch()} loading={list.isFetching}>刷新</Button></Space></div>
    {list.isError ? <Alert type="error" title={list.error.message} action={<Button onClick={() => void list.refetch()}>重试</Button>} /> : <Table rowKey="id" size="small" loading={list.isPending} columns={columns} dataSource={list.data?.items || []} scroll={{ x: 1900 }} pagination={false} locale={{ emptyText: <Empty description="当前筛选范围内没有任务；从手动工具开始执行后会自动记录。" /> }} />}
    <div className="task-pagination"><Text type="secondary">第 {page + 1} 页 · 每页 50 条 · 按创建时间倒序</Text><Space><Button disabled={page === 0} onClick={() => setPage(page - 1)}>上一页</Button>
      <Button disabled={!list.data?.next_cursor} onClick={() => { setCursors(previous => [...previous.slice(0, page + 1), list.data!.next_cursor]); setPage(page + 1) }}>下一页</Button></Space></div>
    <TaskInspector id={selected} close={() => setSelected(null)} inspect={setSelected} />
  </section>
}
