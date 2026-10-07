import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Checkbox, Input, Modal, Select, Space, Typography } from 'antd'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { agentRequest, agentWrite, requestId, type AgentRef, type AuditTask } from './api'

type Candidate = { title: string; reference: AgentRef; required_assets: { asset_id: string; name: string }[]; related_to_task?: boolean }
type Candidates = { items: Candidate[]; next_offset: number | null; unavailable: { reason: string }[] }
type NativeCatalog = { links: { application: 'ocr' | 'classification'; link_id: string; project_id: string }[];
  items: { kind: string; object_id?: string; id?: string; title?: string; state?: string; status?: string; created_at?: string }[]; next_offset: number | null }
type ReturnPayload = { expected_revision: number; summary: string; references: AgentRef[]; include_asset_ids: string[] }
type ReturnAttempt = { stamp: string; request_id: string; payload: ReturnPayload }
function pendingReturn(id: string | null): ReturnAttempt | null {
  if (!id) return null
  try {
    const saved = JSON.parse(sessionStorage.getItem(`clear-manual-return:${id}`) || 'null')
    if (!saved || typeof saved.stamp !== 'string' || typeof saved.request_id !== 'string') return null
    const payload = JSON.parse(saved.stamp)
    if (!Number.isInteger(payload.expected_revision) || typeof payload.summary !== 'string' || !Array.isArray(payload.references) || !Array.isArray(payload.include_asset_ids)) return null
    return { stamp: saved.stamp, request_id: saved.request_id, payload }
  } catch { return null }
}
export const hasPendingManualReturn = (id: string | null) => !!pendingReturn(id)

export function ManualReturn({ taskId, workspaceId, compact = false }: { taskId?: string; workspaceId?: string; compact?: boolean } = {}) {
  const [params] = useSearchParams(), navigate = useNavigate()
  const client = useQueryClient()
  const id = taskId || params.get('task_return'), workspace = workspaceId || params.get('workspace')
  const [open, setOpen] = useState(false), [selected, setSelected] = useState<Record<string, Candidate>>({}), [summary, setSummary] = useState('已完成人工处理，请读取带回的最新结果，继续审计任务。'), [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const [pageOffsets, setPageOffsets] = useState([0]), [keyword, setKeyword] = useState('')
  const [nativeLink, setNativeLink] = useState<string>(), [nativeOffsets, setNativeOffsets] = useState([0])
  const [includeOthers, setIncludeOthers] = useState(false), [expanded, setExpanded] = useState(false)
  const [pending, setPending] = useState(() => pendingReturn(id))
  useEffect(() => { setPending(pendingReturn(id)); setOpen(false); setSelected({}); setError('') }, [id, workspace])
  const offset = pageOffsets[pageOffsets.length - 1]
  const task = useQuery({ queryKey: ['audit-task-return', id], enabled: !!id,
    queryFn: () => agentRequest<AuditTask>(`/agent/sessions/${id}/task`), refetchInterval: 3000 })
  const results = useQuery({ queryKey: ['audit-manual-results', id, !includeOthers, offset, keyword], enabled: !!id && open && task.data?.manual,
    queryFn: () => agentRequest<Candidates>(`/agent/sessions/${id}/manual-results?offset=${offset}&limit=50&keyword=${encodeURIComponent(keyword)}&related_only=${!includeOthers}`) })
  const nativeCatalog = useQuery({ queryKey: ['audit-native-results', id, nativeLink, nativeOffsets], enabled: !!id && open && expanded && task.data?.manual,
    queryFn: () => agentRequest<NativeCatalog>(`/agent/sessions/${id}/native-results${nativeLink ? `?application=${nativeLink.split(':')[0]}&link_id=${encodeURIComponent(nativeLink.split(':')[1])}&offset=${nativeOffsets[nativeOffsets.length - 1]}` : ''}`) })
  async function attachNative(item: NativeCatalog['items'][number]) {
    if (!nativeLink) return
    setBusy(true); setError('')
    try {
      await agentWrite(`/agent/sessions/${id}/native-results/attach`, { request_id: requestId(),
        application: nativeLink.split(':')[0], link_id: nativeLink.split(':')[1], object_id: item.object_id || item.id })
      await client.invalidateQueries({ queryKey: ['audit-manual-results', id] })
    } catch (e) { setError((e as Error).message) }
    finally { setBusy(false) }
  }
  const chosen = Object.values(selected)
  const included = [...new Map(chosen.flatMap(item => item.required_assets || []).map(asset => [asset.asset_id, asset])).values()]
  const mergedAssets = task.data?.selected_assets.length ? [...new Set([...task.data.selected_assets, ...included.map(asset => asset.asset_id)])] : []
  const show = () => {
    setSelected({}); setPageOffsets([0]); setKeyword(''); setIncludeOthers(false); setExpanded(false); setNativeLink(undefined); setNativeOffsets([0]); setOpen(true)
    void client.invalidateQueries({ queryKey: ['audit-manual-results', id] })
  }
  if (!id || !workspace) return null
  const href = `/?workspace=${workspace}&session=${id}`
  const takeOver = async () => {
    if (!task.data) return
    try { await agentWrite(`/agent/sessions/${id}/task/control`, { request_id: requestId(), expected_revision: task.data.revision, action: 'take_over' }); void task.refetch() }
    catch (e) { setError((e as Error).message) }
  }
  const returnToAgent = async (replay = false) => {
    if (!task.data || task.data.workspace_id !== workspace) return
    setBusy(true); setError('')
    const key = `clear-manual-return:${id}`
    const old = pendingReturn(id)
    if (old && !replay) { setError('原带回操作回包待核实，请先核实原操作。'); setBusy(false); return }
    const payload: ReturnPayload = replay && old ? old.payload : { expected_revision: task.data.revision, summary, references: chosen.map(item => item.reference), include_asset_ids: included.map(asset => asset.asset_id) }
    const stamp = JSON.stringify(payload), attempt = old || { stamp, request_id: requestId(), payload }
    setPending(attempt)
    sessionStorage.setItem(key, JSON.stringify(attempt))
    try {
      await agentWrite(`/agent/sessions/${id}/manual-return`, { ...payload, request_id: attempt.request_id })
      const assets = task.data.selected_assets.length ? [...new Set([...task.data.selected_assets, ...payload.include_asset_ids])] : []
      localStorage.setItem(`clear-audit-draft:${workspace}:${id}`, JSON.stringify({ goal: payload.summary, assets }))
      await client.invalidateQueries({ queryKey: ['audit-task', id] })
      await client.invalidateQueries({ queryKey: ['audit-task-return', id] })
      await client.invalidateQueries({ queryKey: ['agent-session', id] })
      sessionStorage.removeItem(key); setPending(null); setOpen(false); navigate(href + '&continue=' + attempt.request_id)
    } catch (e) { setError((e as Error).message); void task.refetch() }
    finally { setBusy(false) }
  }
  return <>{compact ? <Button disabled={busy || !task.data || !pending && !task.data.manual || task.data?.workspace_id !== workspace} onClick={() => pending ? void returnToAgent(true) : show()}>{pending ? '核实原带回' : '带回结果'}</Button> : <Alert className="audit-return-bar" type="info" title={task.data?.manual ? `人工处理中 · ${task.data.title}` : `关联审计任务 · ${task.data?.title || '正在读取'}`}
    description={task.data?.manual ? '保存工具里的修改，再选择最新结果带回。仍在运行的原生作业可继续完成。' : '可以查看结果；修改前接管任务，停止 Agent 的后续动作。'}
    action={<Space><Link to={href}>查看对话</Link>{pending ? <Button loading={busy} disabled={!task.data || task.data.workspace_id !== workspace} onClick={() => void returnToAgent(true)}>核实原带回</Button> : task.data?.manual ? <Button onClick={show}>带回结果</Button> : <Button disabled={!task.data || task.data.workspace_id !== workspace} onClick={() => void takeOver()}>手动接管</Button>}</Space>} />}
    {error && !open && <Alert type="error" title={error} />}
    <Modal title="带回人工处理结果" open={open} onCancel={() => setOpen(false)} okText={pending ? '核实原带回' : '返回 Agent'} cancelText="继续手动处理" okButtonProps={{ loading: busy, disabled: !pending && (!summary.trim() || !results.data || mergedAssets.length > 100) }} onOk={() => void returnToAgent(!!pending)}>
      {pending && <Alert type="warning" title="原带回回包待核实，将使用原结果版本和原请求核实。" />}
      <Typography.Paragraph>选择你修改或生成的版本，Agent 会在继续时读取这些结果。</Typography.Paragraph>
      <Typography.Paragraph type="secondary">{includeOthers ? '正在显示项目内可带回的结果；只有勾选并提交的版本才会采用。' : '默认只显示与当前任务资料或已有结果相关的版本。'}</Typography.Paragraph>
      <Input.Search aria-label="搜索可带回结果" placeholder="搜索作业或资料名称" onSearch={value => { setKeyword(value.trim()); setPageOffsets([0]) }} allowClear />
      {(error || results.error) && <Alert type="error" title={error || results.error?.message} />}
      <div className="audit-asset-picker">{results.data?.items.map(item => {
        const key = JSON.stringify(item.reference)
        return <Checkbox key={key} checked={!!selected[key]} disabled={!!pending || busy || !selected[key] && chosen.length >= 50} onChange={e => setSelected(old => {
          const next = { ...old }; if (e.target.checked) next[key] = item; else delete next[key]; return next
        })}>{item.title}<small>版本 {String(item.reference.revision)}</small>{!!item.required_assets?.length && <small>选中此结果会加入资料：{item.required_assets.map(asset => asset.name).join('、')}</small>}</Checkbox>
      })}</div>
      <Space><Button disabled={pageOffsets.length === 1 || results.isFetching} onClick={() => setPageOffsets(old => old.slice(0, -1))}>上一页</Button>
        <Button disabled={results.data?.next_offset == null || results.isFetching} onClick={() => setPageOffsets(old => [...old, results.data!.next_offset!])}>下一页</Button>
        <Typography.Text type="secondary">已选 {chosen.length} 项，最多 50 项</Typography.Text></Space>
      {!!included.length && <Alert type="info" title="同时加入任务资料范围" description={included.map(asset => asset.name).join('、')} />}
      <details className="audit-return-sources" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}><summary>更多来源</summary>
      <Checkbox disabled={!!pending || busy} checked={includeOthers} onChange={event => { setIncludeOthers(event.target.checked); setPageOffsets([0]) }}>显示项目内其它结果</Checkbox>
      <Typography.Text strong>接回工具内独立导入的结果</Typography.Text>
      <Select aria-label="选择原生项目" disabled={!!pending || busy} placeholder="选择 OCR 或分类项目" allowClear style={{ width: '100%', marginBlock: 8 }} value={nativeLink}
        options={nativeCatalog.data?.links?.map(link => ({ value: `${link.application}:${link.link_id}`, label: `${link.application === 'ocr' ? 'OCR' : '分类'} · ${link.project_id}` }))}
        onChange={value => { setNativeLink(value); setNativeOffsets([0]) }} />
      {nativeCatalog.error && <Alert type="warning" title="原生工具暂不可读取" description="启动对应工具并保存结果后，重新选择项目。" />}
      {nativeLink && <div className="audit-asset-picker">{nativeCatalog.data?.items?.filter(item => item.kind === 'result' || item.kind === 'inference').map(item =>
        <Space key={item.object_id || item.id} style={{ display: 'flex', justifyContent: 'space-between' }}>
          <Typography.Text>{item.title || `分类推理 · ${item.created_at || item.id}`}</Typography.Text>
          <Button disabled={!!pending} loading={busy} onClick={() => void attachNative(item)}>核实原件并接回</Button>
        </Space>)}
        <Space><Button disabled={nativeOffsets.length === 1 || nativeCatalog.isFetching} onClick={() => setNativeOffsets(old => old.slice(0, -1))}>上一页</Button>
          <Button disabled={nativeCatalog.data?.next_offset == null || nativeCatalog.isFetching} onClick={() => setNativeOffsets(old => [...old, nativeCatalog.data!.next_offset!])}>下一页</Button></Space>
        <Typography.Paragraph type="secondary">接回后在下方选择结果；新增资料会明确列出，随所选结果加入任务范围。</Typography.Paragraph>
      </div>}
      </details>
      {results.data?.unavailable.length ? <Typography.Paragraph type="secondary">部分结果尚不可读取或不在本任务资料范围。先在工具中保存，再点击刷新。</Typography.Paragraph> : null}
      <Button onClick={() => { setSelected({}); setPageOffsets([0]); void client.invalidateQueries({ queryKey: ['audit-manual-results', id] }) }}>刷新结果</Button>
      <label className="audit-field">改动摘要<Input.TextArea aria-label="人工改动摘要" rows={3} value={summary} disabled={!!pending || busy} maxLength={4000} onChange={e => setSummary(e.target.value)} /></label>
    </Modal></>
}
