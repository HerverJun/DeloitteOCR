import { useEffect, useMemo, useRef, useState, type ComponentRef } from 'react'
import { Alert, Button, Card, Drawer, Input, Select, Space, Tag, Typography } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { ControllerSettings } from './ControllerSettings'
import { compactionNotice, ContextBudgetContent, ContextCompactionContent, DeliveryCheckContent, EnvironmentContent, ModelRequestContent, ModelUsageContent } from './ContextBudget'
import { SkillsPanel } from './SkillsPanel'
import { PlatformTools } from './PlatformTools'
import { AgentPreferences } from './AgentPreferences'
import { ManualReturn, hasPendingManualReturn } from './ManualReturn'
import { AgentRequestError, agentRequest, agentWrite, requestId, type AgentEvent, type AgentRun, type AgentSession, type Controller, type AgentRef, type AuditTask, type AuditProfile, type ContextBudget, type DeliveryPolicy } from './api'
import { AgentMarkdown, AgentMessage } from './AgentMessage'
import { ToolActivity } from './ToolActivity'
import { RunStatus } from './RunStatus'
import { useAgentEvents } from './useAgentEvents'
import { useConversationScroll } from './useConversationScroll'
import { terminalActivity } from './conversationProjection'
import { Materials, ProjectBackground } from './Materials'
import { RecentTasks, TaskPanel, ReferenceLinks, statusLabels } from './TaskPanel'
import { createWorkbenchView } from './workbenchView'
import { FileTextTools } from './FileTextTools'
import { documentsApi } from '../documents/api'
import { taskIntents } from '../home/taskIntents'
import { useWorkspaces } from '../../App'
import './agent.css'
import './workbench-visual.css'
import './experience.css'
import { rememberVisit } from '../../clear-ui/recentVisits'
import { useSession } from '../tasks/session'
import type { Workspace } from '../../../../../packages/contracts/types.generated'
import ArrowUpOutlined from '@ant-design/icons/es/icons/ArrowUpOutlined'
import PlusOutlined from '@ant-design/icons/es/icons/PlusOutlined'
import HistoryOutlined from '@ant-design/icons/es/icons/HistoryOutlined'
import LayoutOutlined from '@ant-design/icons/es/icons/LayoutOutlined'
import DownOutlined from '@ant-design/icons/es/icons/DownOutlined'

const labels: Record<string, string> = { ...statusLabels, running: '处理中', waiting_job: '等待工具完成', waiting_user: '等待补充信息',
  needs_verification: '需要核实', succeeded: '已完成', partial: '部分成果', cancelled: '已停止', incomplete: '未形成交付', failed: '未完成',
  pending: '处理中', success: '已完成', error: '未完成', needs_user: '等待补充信息' }
type DeliveredFile = { name: string; download_url: string }
type DeliveredBundle = { bundle_id: string; title: string; files: DeliveredFile[] }

export function EventContent({ event, reply, answered, answerable, taskId }: { event: AgentEvent; reply: (id: string, revision: number, content: string) => void; answered: boolean; answerable: boolean; taskId?: string }) {
  const [answer, setAnswer] = useState('')
  const payload = event.payload
  if (event.kind === 'message' && payload.role === 'assistant' && !String(payload.content || '').trim()) return null
  if (event.kind === 'message') return <div className={`agent-message ${payload.role === 'user' ? 'agent-message-user' : ''}`}>
    <Typography.Text type="secondary">{payload.role === 'user' ? '你' : 'Agent'}</Typography.Text>
    {payload.role === 'user' ? <Typography.Paragraph>{String(payload.content || '')}</Typography.Paragraph> : <AgentMarkdown content={String(payload.content || '')} />}
    {Array.isArray(payload.references) && <ReferenceLinks refs={payload.references as AgentRef[]} taskId={taskId} />}
    {payload.queued === true && <Tag>将在下一处理边界应用</Tag>}
  </div>
  if (event.kind === 'tool_step') return <Card size="small" className="agent-step">
    <Space><Tag>{labels[String(payload.status)] || String(payload.status)}</Tag><Typography.Text>{String(payload.summary || '')}</Typography.Text></Space>
    <details><summary>查看操作</summary><pre>{JSON.stringify(payload, null, 2)}</pre></details>
  </Card>
  if (event.kind === 'question') return <Card size="small" className="agent-question" title={String(payload.question)}>
    {Array.isArray(payload.references) && <ReferenceLinks refs={payload.references as AgentRef[]} taskId={taskId} />}
    {(answered || !answerable) && <Typography.Paragraph type="secondary">{answered ? '已答复' : '此问题当前不可答复'}</Typography.Paragraph>}
    <Space wrap>{((payload.options || []) as string[]).map(option => <Button key={option}
      disabled={answered || !answerable}
      onClick={() => reply(String(payload.decision_id), Number(payload.revision), option)}>{option}</Button>)}</Space>
    <Space.Compact block style={{ marginTop: 12 }}><Input disabled={answered || !answerable} value={answer} onChange={event => setAnswer(event.target.value)} placeholder="补充你的选择" />
      <Button disabled={!answer.trim() || answered || !answerable} onClick={() => { reply(String(payload.decision_id), Number(payload.revision), answer); setAnswer('') }}>回答</Button></Space.Compact>
  </Card>
  if (event.kind === 'delivery') {
    const delivery = payload.delivery as { result_refs?: AgentRef[]; artifact_refs?: AgentRef[]; data?: { delivery?: DeliveredBundle[]; limitations?: string[]; draft?: boolean; review_state?: string } } | null
    const unreviewed = delivery?.data?.draft === true || delivery?.data?.review_state === 'unreviewed'
    return <Card title={unreviewed ? '未复核草稿' : labels[String(payload.status)] || '成果'} size="small">
      {unreviewed && <Alert type="warning" title="此交付包含未人工核准的算法结果或草稿字段" description="可用于草稿分析；正式结论与核准字段仍需用户确认。" />}
      <Typography.Paragraph>{String(payload.content || '')}</Typography.Paragraph>
      <ReferenceLinks refs={[...(delivery?.result_refs || []), ...(delivery?.artifact_refs || [])]} taskId={taskId} />
      {delivery?.data?.delivery?.map(bundle => <div key={bundle.bundle_id}><Typography.Text strong>{bundle.title}</Typography.Text>
        <div className="agent-downloads">{bundle.files.map(file => <Button key={file.name} href={file.download_url}>{file.name}</Button>)}</div></div>)}
      {delivery?.data?.limitations?.map((text, index) => <Typography.Paragraph type="secondary" key={index}>{text}</Typography.Paragraph>)}
    </Card>
  }
  if (event.kind === 'model_request') return <ModelRequestContent payload={payload} />
  if (event.kind === 'usage') return <ModelUsageContent payload={payload} />
  if (event.kind === 'context_injection') return <EnvironmentContent payload={payload} />
  if (event.kind === 'context_compaction') return <ContextCompactionContent payload={payload} />
  if (event.kind === 'delivery_check') return <DeliveryCheckContent payload={payload} />
  if (event.kind === 'error') return <div><Alert type="error" title={String(payload.message || '处理未完成')} />
    {!!(payload.context_budget || payload.context_metrics) && <ContextBudgetContent budget={(payload.context_budget || payload.context_metrics) as ContextBudget} exceeded />}</div>
  if (event.kind === 'cancelled') return <Alert type="info" title="已停止后续步骤，已有成果保留" />
  if (event.kind === 'task_control') return <Typography.Paragraph type="secondary">{payload.manual ? '已切换人工处理，原生作业与结果保留。' : '已返回智能审计，可以带入新结果继续。'}</Typography.Paragraph>
  if (event.kind === 'model_delta') return <div className="agent-message"><Typography.Text type="secondary">Agent · 正在回应</Typography.Text><Typography.Paragraph>{String(payload.content || '')}</Typography.Paragraph></div>
  return null
}

export function AgentWorkbench() {
  const [params, setParams] = useSearchParams()
  const workspace = params.get('workspace'), sessionId = params.get('session')
  const [draftAssets, setDraftAssets] = useState<string[]>((params.get('assets') || '').split(',').filter(Boolean))
  const [draftReferenceIds, setDraftReferenceIds] = useState<string[]>([]), [platformTools, setPlatformTools] = useState(false)
  const [draftPolicy, setDraftPolicy] = useState<DeliveryPolicy>('auto')
  const assetSetSource = params.get('asset_set')
  const bundle = params.get('bundle'), sourceResult = params.get('result'), sourceSession = params.get('data_session'), sourceDocument = params.get('document')
  const [settings, setSettings] = useState(false), [skills, setSkills] = useState(false), [goal, setGoal] = useState('')
  const [preferences, setPreferences] = useState(false)
  const [directTools, setDirectTools] = useState(false)
  const [materials, setMaterials] = useState(false), [background, setBackground] = useState(false), [recent, setRecent] = useState(false), [keyword, setKeyword] = useState('')
  const [error, setError] = useState(''), [notice, setNotice] = useState(''), [controlling, setControlling] = useState(false), [taskDrawer, setTaskDrawer] = useState(false)
  const [pendingControl, setPendingControl] = useState<'take_over' | 'return_to_agent' | null>(null)
  const [narrow, setNarrow] = useState(() => !!window.matchMedia?.('(max-width: 680px)').matches)
  const [wide, setWide] = useState(() => !!window.matchMedia?.('(min-width: 1280px)').matches)
  const [panelVisible, setPanelVisible] = useState(() => localStorage.getItem('clear-agent-task-panel-v2') === 'open')
  const [diagnostics, setDiagnostics] = useState(false)
  const [offeredIntent, setOfferedIntent] = useState<typeof taskIntents[number] | null>(null)
  const goalInput = useRef<ComponentRef<typeof Input.TextArea>>(null)
  const actionMenu = useRef<HTMLDetailsElement>(null), panelToggle = useRef<HTMLButtonElement>(null)
  const actionScope = useRef(`${workspace}:${sessionId}`); actionScope.current = `${workspace}:${sessionId}`
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const client = useQueryClient()
  const { user } = useSession()
  const projects = useWorkspaces(recent && !workspace)
  const composing = useRef(false)
  const health = useQuery({ queryKey: ['agent-health'], queryFn: () => agentRequest<{ enabled: boolean; direct_enabled?: boolean; reason: string; engine?: string; engine_version?: string }>('/agent/health') })
  const neutralAvailable = !!(health.data?.direct_enabled ?? health.data?.enabled)
  const { projection, events, subscription, reconnect } = useAgentEvents(workspace, sessionId, neutralAvailable)
  const connection = useQuery({ queryKey: ['agent-connection'], enabled: !!health.data?.enabled,
    queryFn: () => agentRequest<Controller>('/agent/connection') })
  const source = useQuery({ queryKey: ['agent-source', workspace, bundle, sourceResult, sourceSession, sourceDocument, assetSetSource],
    enabled: !!workspace && !!(bundle || assetSetSource || sourceResult && sourceSession || sourceDocument) && !sessionId && !!health.data?.enabled,
    queryFn: async (): Promise<{ reference: AgentRef }> => {
      if (sourceDocument) {
        const run = await documentsApi.get(workspace!, sourceDocument)
        if (!run.document || !run.result_hash || !run.source?.blob_hash || !['succeeded', 'partial'].includes(run.status)) throw new Error('解析文档尚无可读取的保存结果。')
        return { reference: { kind: 'result', workspace_id: workspace!, object_id: run.document_id, revision: run.document.revision, sha256: run.result_hash, application: 'platform', parent_id: run.document_id, coverage: { kind: 'documents' },
          sources: [{ kind: 'asset', workspace_id: workspace!, object_id: run.source.asset_id, revision: run.source.asset_revision, sha256: run.source.blob_hash }] } }
      }
      return agentWrite<{ reference: AgentRef }>(`/workspaces/${workspace}/agent/references`, assetSetSource ? { kind: 'asset_set', object_id: assetSetSource } : bundle ? { kind: 'artifact', object_id: bundle } : { kind: 'result', object_id: sourceResult, parent_id: sourceSession })
    } })
  const listing = useQuery({ queryKey: ['audit-tasks', workspace, keyword], enabled: !!workspace && neutralAvailable,
    queryFn: () => agentRequest<{ items: AuditTask[] }>(`/workspaces/${workspace}/audit-tasks?keyword=${encodeURIComponent(keyword)}`), refetchInterval: 4000 })
  const task = useQuery({ queryKey: ['audit-task', sessionId], enabled: !!sessionId && neutralAvailable,
    queryFn: () => agentRequest<AuditTask>(`/agent/sessions/${sessionId}/task`), refetchInterval: 1500 })
  const profile = useQuery({ queryKey: ['audit-profile', workspace], enabled: !!workspace && !!health.data?.enabled,
    queryFn: () => agentRequest<AuditProfile>(`/workspaces/${workspace}/audit-profile`) })
  const pendingScope = task.data?.pending_scope as { selected_assets?: string[]; authorized_reference_ids?: string[] } | null | undefined
  const selectedAssets = pendingScope?.selected_assets ?? task.data?.selected_assets ?? draftAssets
  const authorizedReferenceIds = pendingScope?.authorized_reference_ids ?? task.data?.authorized_reference_ids ?? draftReferenceIds
  const deliveryPolicy = task.data?.delivery_policy ?? draftPolicy
  const session = useQuery({ queryKey: ['agent-session', sessionId], enabled: !!sessionId && neutralAvailable,
    queryFn: () => agentRequest<AgentSession>(`/agent/sessions/${sessionId}`), refetchInterval: 1500 })
  const latestRun = session.data?.runs?.[0]
  const active = session.data?.runs?.find(run => ['running', 'waiting_job', 'waiting_user', 'needs_verification', 'paused'].includes(run.status) && !terminalActivity(projection.activities[run.id]?.phase || ''))
    || (['failed', 'partial'].includes(latestRun?.status || '') && latestRun?.can_resume ? latestRun : undefined)
  const canStop = !!active && ['running', 'waiting_job', 'waiting_user', 'needs_verification', 'paused', 'partial'].includes(active.status)
  const mismatch = !!session.data && !!workspace && session.data.workspace_id !== workspace
  const answered = new Set(events.filter(event => event.kind === 'question_answered' || event.payload.decision_id && event.kind === 'message')
    .map(event => String(event.payload.decision_id)))
  const timelineIds = useMemo(() => projection.items.map(item => item.id), [projection.items])
  const scroll = useConversationScroll(`${workspace}:${sessionId}`, timelineIds, events.at(-1)?.seq || 0, subscription.status === 'connected')
  const activity = active ? projection.activities[active.id] : Object.values(projection.activities).sort((a, b) => b.seq - a.seq)[0]
  const clearSource = (next: URLSearchParams) => { ['assets', 'bundle', 'result', 'data_session', 'document', 'asset_set', 'intent'].forEach(key => next.delete(key)) }
  const choose = (id: string) => { const next = new URLSearchParams(params); next.set('session', id); clearSource(next); setGoal(''); setParams(next); setRecent(false) }
  const draftKey = `clear-audit-draft:${workspace || 'new'}:${sessionId || 'new'}`
  useEffect(() => { setOfferedIntent(null) }, [draftKey])
  useEffect(() => {
    const draft = localStorage.getItem(draftKey)
    try {
      const value = draft ? JSON.parse(draft) : null
      const previousGoal = typeof value?.goal === 'string' ? value.goal : ''
      const preset = !sessionId ? taskIntents.find(item => item.id === params.get('intent')) : undefined
      const nextGoal = previousGoal || preset?.draft || ''
      const assets = value?.assets || (params.get('assets') || '').split(',').filter(Boolean)
      setGoal(nextGoal); setDraftAssets(assets); setDraftReferenceIds(value?.authorized_reference_ids || [])
      setDraftPolicy(['auto', 'draft', 'reviewed'].includes(value?.delivery_policy) ? value.delivery_policy : 'auto')
      if (preset) {
        if (previousGoal.trim()) setOfferedIntent(preset)
        else localStorage.setItem(draftKey, JSON.stringify({ ...value, goal: nextGoal, assets }))
        goalInput.current?.focus()
      }
    } catch { setGoal(''); setDraftReferenceIds([]); setDraftPolicy('auto') }
    if (params.has('intent')) { const next = new URLSearchParams(params); next.delete('intent'); setParams(next, { replace: true }) }
  }, [draftKey, params.get('continue'), params.get('intent')])
  const writeGoal = (value: string) => { setGoal(value); localStorage.setItem(draftKey, JSON.stringify({ goal: value, assets: selectedAssets, authorized_reference_ids: authorizedReferenceIds, delivery_policy: deliveryPolicy })) }
  const createProject = async (entryWorkspace = workspace, projectGoal = goal) => {
    if (entryWorkspace) return entryWorkspace
    const key = 'clear-audit-new-project'
    const attempt = JSON.parse(sessionStorage.getItem(key) || 'null') as { request_id: string; name?: string } | null
    const baseName = `${projectGoal.trim().replace(/\s+/g, ' ').slice(0, 40) || '资料处理'} · ${new Date().toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })}`
    const existingNames = new Set(projects.data?.map(project => project.name))
    let name = baseName, suffix = 2
    while (existingNames.has(name)) name = `${baseName} (${suffix++})`
    const command = attempt || { request_id: requestId(), name }, pending = JSON.stringify(command)
    sessionStorage.setItem(key, pending)
    // Old pending requests used the fixed name; keep their original payload when verifying them.
    const created = await agentWrite<Workspace>('/workspaces', { schema_version: '1.0', name: command.name || '智能审计项目', request_id: command.request_id })
    if (sessionStorage.getItem(key) === pending) sessionStorage.removeItem(key)
    client.setQueryData<Workspace[]>(['workspaces'], previous => [...(previous || []).filter(item => item.workspace_id !== created.workspace_id), created])
    void client.invalidateQueries({ queryKey: ['workspaces'] }); return created.workspace_id
  }
  const openProjectAction = async (kind: 'materials' | 'background') => {
    const scope = actionScope.current, entryParams = params.toString()
    const draft = { goal, assets: [...selectedAssets], authorized_reference_ids: [...authorizedReferenceIds], delivery_policy: deliveryPolicy }
    try {
      if (kind === 'background') { setBackground(true); return }
      const project = await createProject()
      if (!workspace) localStorage.setItem(`clear-audit-draft:${project}:new`, JSON.stringify(draft))
      if (!mounted.current || scope !== actionScope.current) return
      if (!workspace) { const next = new URLSearchParams(entryParams); next.set('workspace', project); setParams(next) }
      setMaterials(true)
    } catch (caught) { if (mounted.current && scope === actionScope.current) setError((caught as Error).message) }
  }
  const refreshTask = () => { if (sessionId) { void task.refetch(); void session.refetch() } void client.invalidateQueries({ queryKey: ['audit-tasks', workspace] }) }
  const saveScope = async (ids: string[]) => {
    if (mismatch) throw new Error('会话与项目不一致，请重新选择任务。')
    if (sessionId && !task.data) throw new Error('正在读取当前任务，请稍后再添加文件。')
    const scope = actionScope.current
    if (sessionId && task.data) {
      await agentWrite(`/agent/sessions/${sessionId}/task`, { request_id: requestId(), expected_revision: task.data.revision, selected_assets: ids }, 'PUT')
      void client.invalidateQueries({ queryKey: ['audit-task', sessionId] })
      void client.invalidateQueries({ queryKey: ['agent-session', sessionId] })
    }
    localStorage.setItem(draftKey, JSON.stringify({ goal, assets: ids, authorized_reference_ids: authorizedReferenceIds, delivery_policy: deliveryPolicy }))
    void client.invalidateQueries({ queryKey: ['audit-tasks', workspace] })
    if (scope === actionScope.current) setDraftAssets(ids)
  }
  const saveReferences = async (ids: string[]) => {
    if (mismatch) throw new Error('会话与项目不一致，请重新选择任务。')
    if (sessionId) {
      if (!task.data) throw new Error('先读取任务状态，再保存参考范围。')
      const key = `clear-agent-reference-scope:${sessionId}`, stamp = JSON.stringify([...ids].sort())
      const original = JSON.parse(sessionStorage.getItem(key) || 'null') as { stamp: string; body: { request_id: string; expected_revision: number; authorized_reference_ids: string[] } } | null
      if (original && original.stamp !== stamp) throw new Error('原参考范围操作的回包待核实，请先重试原选择。')
      const attempt = original || { stamp, body: { request_id: requestId(), expected_revision: task.data.revision, authorized_reference_ids: ids } }
      sessionStorage.setItem(key, JSON.stringify(attempt))
      try { await agentWrite(`/agent/sessions/${sessionId}/task`, attempt.body, 'PUT'); sessionStorage.removeItem(key) }
      catch (caught) { if (caught instanceof AgentRequestError && [400, 403, 404, 409, 422].includes(caught.status)) sessionStorage.removeItem(key); throw caught }
    }
    setDraftReferenceIds(ids); localStorage.setItem(draftKey, JSON.stringify({ goal, assets: selectedAssets, authorized_reference_ids: ids, delivery_policy: deliveryPolicy })); refreshTask()
  }
  const saveDeliveryPolicy = async (policy: DeliveryPolicy) => {
    if (mismatch) throw new Error('会话与项目不一致，请重新选择任务。')
    if (sessionId) {
      if (!task.data) throw new Error('先读取任务状态，再保存交付要求。')
      const key = `clear-agent-delivery-policy:${sessionId}`
      const original = JSON.parse(sessionStorage.getItem(key) || 'null') as { request_id: string; expected_revision: number; delivery_policy: DeliveryPolicy } | null
      if (original && original.delivery_policy !== policy) throw new Error('原交付要求的回包待核实，请先重试原选择。')
      const attempt = original || { request_id: requestId(), expected_revision: task.data.revision, delivery_policy: policy }
      sessionStorage.setItem(key, JSON.stringify(attempt))
      try { await agentWrite(`/agent/sessions/${sessionId}/task`, attempt, 'PUT'); sessionStorage.removeItem(key) }
      catch (caught) { if (caught instanceof AgentRequestError && [400, 403, 404, 409, 422].includes(caught.status)) sessionStorage.removeItem(key); throw caught }
    }
    setDraftPolicy(policy); localStorage.setItem(draftKey, JSON.stringify({ goal, assets: selectedAssets, authorized_reference_ids: authorizedReferenceIds, delivery_policy: policy })); refreshTask()
  }
  const control = async (manual: boolean) => {
    if (!task.data || controlling || mismatch) return
    const scope = actionScope.current, id = sessionId!, key = `clear-agent-control:${id}`, action = manual ? 'take_over' : 'return_to_agent'
    let command: { request_id: string; expected_revision: number; action: string }
    try { command = JSON.parse(sessionStorage.getItem(key) || 'null') || { request_id: requestId(), expected_revision: task.data.revision, action }
      if (command.action !== action) throw new Error('原控制操作回包待核实，请先重试原操作。')
    } catch (caught) { setError((caught as Error).message); return }
    sessionStorage.setItem(key, JSON.stringify(command)); setPendingControl(action); setControlling(true)
    try { await agentWrite(`/agent/sessions/${id}/task/control`, command); sessionStorage.removeItem(key); if (actionScope.current === scope) setPendingControl(null) }
    catch (caught) { if (caught instanceof AgentRequestError && [400, 403, 404, 409].includes(caught.status)) { sessionStorage.removeItem(key); if (actionScope.current === scope) setPendingControl(null) } if (actionScope.current === scope) setError((caught as Error).message) }
    finally { void client.invalidateQueries({ queryKey: ['audit-task', id] }); void client.invalidateQueries({ queryKey: ['agent-session', id] }); if (actionScope.current === scope) setControlling(false) }
  }
  useEffect(() => {
    try {
      const stored = sessionStorage.getItem('clear-agent-pending-send')
      if (!stored) return
      const pending = JSON.parse(stored) as { stamp: string; content?: string }
      const identity = JSON.parse(pending.stamp) as [string | null, string | null, string[], string, unknown]
      if (identity[0] === workspace && identity[1] === sessionId) setGoal(pending.content || identity[3])
    } catch { /* An invalid stale draft cannot authorize a write. */ }
  }, [workspace, sessionId])
  useEffect(() => { setError(''); setNotice(''); setControlling(false); setTaskDrawer(false); setDiagnostics(false); setDirectTools(false)
    try { const pending = JSON.parse(sessionStorage.getItem(`clear-agent-control:${sessionId}`) || 'null') as { action?: string } | null
      setPendingControl(pending?.action === 'take_over' || pending?.action === 'return_to_agent' ? pending.action : null)
    } catch { setPendingControl(null) }
  }, [workspace, sessionId])
  const captureSend = (content: string, execute: boolean) => ({ content, execute, workspace, sessionId, scope: actionScope.current, draftKey, params: params.toString(),
    selectedAssets: [...selectedAssets], authorizedReferenceIds: [...authorizedReferenceIds], deliveryPolicy,
    references: source.data ? [structuredClone(source.data.reference)] : [], mismatch,
    missingSource: !!((bundle || sourceResult || sourceDocument || assetSetSource) && !sessionId && !source.data) })
  const send = useMutation({ mutationFn: async (entry: ReturnType<typeof captureSend>) => {
    const { content, execute, workspace: entryWorkspace, sessionId: entrySession, selectedAssets: assets, authorizedReferenceIds: referenceIds, deliveryPolicy: policy, references } = entry
    if (entry.mismatch) throw new Error('会话属于其他项目，请重新选择会话')
    if (entry.missingSource) throw new Error('先核实带入成果的来源和版本')
    const stamp = JSON.stringify([entryWorkspace, entrySession, assets, content, references, referenceIds, policy])
    const stored = sessionStorage.getItem('clear-agent-pending-send')
    let attempt = stored ? JSON.parse(stored) as { stamp: string; content: string; project: string | null; conversation: string | null; projectRequest: string; sessionRequest: string; messageRequest: string } : null
    if (!attempt || attempt.stamp !== stamp) attempt = { stamp, content, project: entryWorkspace, conversation: entrySession,
      projectRequest: requestId(), sessionRequest: requestId(), messageRequest: requestId() }
    const retain = () => sessionStorage.setItem('clear-agent-pending-send', JSON.stringify(attempt))
    retain()
    let project = attempt.project, conversation = attempt.conversation
    if (!project) {
      project = await createProject(entryWorkspace, content)
      attempt.project = project; retain()
      void client.invalidateQueries({ queryKey: ['workspaces'] })
    }
    if (!conversation) {
      const created = await agentWrite<AgentSession>(`/workspaces/${project}/agent/sessions`,
        { name: content.slice(0, 60), selected_assets: assets, authorized_reference_ids: referenceIds, delivery_policy: policy, initial_refs: references, request_id: attempt.sessionRequest })
      conversation = created.id
      attempt.conversation = conversation; retain()
    }
    if (execute) await agentWrite<AgentRun>(`/agent/sessions/${conversation}/messages`, { content, request_id: attempt.messageRequest })
    else { const current = await agentRequest<AuditTask>(`/agent/sessions/${conversation}/task`)
      await agentWrite(`/agent/sessions/${conversation}/task`, { request_id: attempt.messageRequest, expected_revision: current.revision, goal: content }, 'PUT') }
    sessionStorage.removeItem('clear-agent-pending-send')
    return { project, conversation, entry }
  }, onSuccess: ({ project, conversation, entry }) => {
    localStorage.removeItem(entry.draftKey)
    localStorage.setItem(`clear-audit-draft:${project}:${conversation}`, JSON.stringify({ goal: entry.execute ? '' : entry.content, assets: entry.selectedAssets, authorized_reference_ids: entry.authorizedReferenceIds, delivery_policy: entry.deliveryPolicy }))
    void client.invalidateQueries({ queryKey: ['audit-tasks', project] }); void client.invalidateQueries({ queryKey: ['agent-session', conversation] }); void client.invalidateQueries({ queryKey: ['audit-task', conversation] })
    if (!mounted.current || entry.scope !== actionScope.current) return
    setGoal(entry.execute ? '' : entry.content); const next = new URLSearchParams(entry.params); next.set('workspace', project); next.set('session', conversation); clearSource(next); setParams(next)
  }, onError: (caught, entry) => { if (mounted.current && entry.scope === actionScope.current) setError(caught.message) } })
  const reply = async (id: string, revision: number, content: string) => {
    const key = `clear-agent-reply-${id}-${revision}`
    const old = sessionStorage.getItem(key)
    const attempt = old ? JSON.parse(old) as { content: string; request_id: string } : { content, request_id: requestId() }
    if (attempt.content !== content) { setError('此问题已有待核实的答复，请先重试原答复'); return }
    sessionStorage.setItem(key, JSON.stringify(attempt))
    try { await agentWrite(`/agent/decisions/${id}/reply`, { ...attempt, revision }); setError(''); void session.refetch() }
    catch (caught) { setError((caught as Error).message) }
  }
  const stop = async (owned: boolean) => {
    if (!active || !canStop || controlling || mismatch) return
    const scope = actionScope.current, id = active.id, conversation = sessionId
    setControlling(true)
    try { await agentWrite(`/agent/runs/${id}/cancel`, { cancel_owned_jobs: owned }) }
    catch (caught) { if (scope === actionScope.current) setError((caught as Error).message) }
    finally { void client.invalidateQueries({ queryKey: ['agent-session', conversation] }); void client.invalidateQueries({ queryKey: ['audit-task', conversation] }); if (scope === actionScope.current) setControlling(false) }
  }
  const changeRun = async (action: 'pause' | 'resume') => {
    if (!active || controlling || mismatch) return
    const scope = actionScope.current, id = active.id, conversation = sessionId
    setControlling(true)
    try { await agentWrite(`/agent/runs/${id}/${action}`, {}); if (scope === actionScope.current) setError('') }
    catch (caught) { if (scope === actionScope.current) setError((caught as Error).message) }
    finally { void client.invalidateQueries({ queryKey: ['agent-session', conversation] }); void client.invalidateQueries({ queryKey: ['audit-task', conversation] }); if (scope === actionScope.current) setControlling(false) }
  }
  const { latestCompaction, latestBudget } = useMemo(() => {
    let latestCompaction: AgentEvent | undefined, latestRequest: AgentEvent | undefined
    for (let index = events.length - 1; index >= 0 && (!latestCompaction || !latestRequest); index--) {
      const event = events[index]
      if (!latestCompaction && event.kind === 'context_compaction') latestCompaction = event
      if (!latestRequest && event.kind === 'model_request') latestRequest = event
    }
    return { latestCompaction, latestBudget: (latestRequest?.payload.context_budget || latestRequest?.payload.context_metrics) as ContextBudget | undefined }
  }, [events])
  const compactionPending = ['queued', 'start', 'summary'].includes(String(latestCompaction?.payload.phase))
  const compact = useMutation({ mutationFn: ({ id }: { id: string; scope: string }) => agentWrite<{ queued?: boolean; compacted?: boolean }>(`/agent/sessions/${id}/compact`, {}),
    onSuccess: (result, command) => { if (command.scope === actionScope.current) { setNotice(compactionNotice(result)); reconnect() } },
    onError: (caught, command) => { if (command.scope === actionScope.current) setError(caught.message) } })
  const newSession = () => { const next = new URLSearchParams(params); next.delete('session'); clearSource(next); setGoal(''); setDraftAssets([]); setDraftReferenceIds([]); setDraftPolicy('auto'); setParams(next); setRecent(false) }
  useEffect(() => { if (!window.matchMedia) return
    const mobile = window.matchMedia('(max-width: 680px)'), desktop = window.matchMedia('(min-width: 1280px)')
    const update = () => { setNarrow(mobile.matches); setWide(desktop.matches); setTaskDrawer(false) }
    update(); mobile.addEventListener?.('change', update); desktop.addEventListener?.('change', update)
    return () => { mobile.removeEventListener?.('change', update); desktop.removeEventListener?.('change', update) }
  }, [])
  useEffect(() => {
    const closeOutside = (event: PointerEvent) => { if (actionMenu.current?.open && !actionMenu.current.contains(event.target as Node)) actionMenu.current.open = false }
    document.addEventListener('pointerdown', closeOutside)
    return () => document.removeEventListener('pointerdown', closeOutside)
  }, [])
  const togglePanel = () => {
    if (!wide) { setTaskDrawer(true); return }
    setPanelVisible(value => { localStorage.setItem('clear-agent-task-panel-v2', value ? 'closed' : 'open'); return !value })
    panelToggle.current?.focus()
  }
  const workbenchView = useMemo(() => createWorkbenchView(task.data, events, projection), [task.data, events, projection])
  const taskPanel = <TaskPanel task={task.data} events={events} projection={projection} view={workbenchView} busy={send.isPending || controlling} hasProject={!!workspace}
    materials={() => { void openProjectAction('materials') }} background={() => { void openProjectAction('background') }}
    skills={() => setSkills(true)} tools={() => setPlatformTools(true)} rename={name => {
      if (task.data && name.trim()) void agentWrite(`/agent/sessions/${sessionId}/task`, { request_id: requestId(), expected_revision: task.data.revision, name: name.trim() }, 'PUT').then(refreshTask).catch(e => setError(e.message))
    }} />
  useEffect(() => {
    if (workspace && sessionId && task.data?.title) rememberVisit(user?.id || 'single', { href: '/agent?workspace=' + encodeURIComponent(workspace) + '&session=' + encodeURIComponent(sessionId), title: task.data.title, workspace })
  }, [workspace, sessionId, task.data?.title, user?.id])
  const sendDisabled = !goal.trim() || controlling || !neutralAvailable || task.data?.manual || mismatch || !!((bundle || sourceResult || sourceDocument) && !sessionId && !source.data)
  const submitGoal = () => { if (!sendDisabled && !send.isPending) { setError(''); send.mutate(captureSend(goal.trim(), true)) } }
  const manualParams = new URLSearchParams(params)
  manualParams.delete('session')
  manualParams.delete('continue')
  if (sessionId) manualParams.set('task_return', sessionId)
  if (selectedAssets.length) manualParams.set('assets', selectedAssets.join(','))
  const manualHref = `/apps${manualParams.size ? `?${manualParams}` : ''}`
  return <div className="agent-workbench">
    <header className="agent-heading"><div className="agent-heading-title"><span className="audit-eyebrow">Agent 工作区</span>
      <div className="agent-heading-identity"><Typography.Title level={2} title={task.data?.title}>{task.data?.title || '新任务'}</Typography.Title>
        <Tag className="agent-model-state" title={connection.data?.model} color={connection.data?.available ? 'success' : 'default'}>{connection.data?.available ? connection.data.model : '模型待连接'}</Tag></div>
      <Typography.Text className="agent-current-task" type="secondary">对话、文件处理与专业分析</Typography.Text></div>
      <nav className="audit-context-bar" aria-label="审计任务操作"><Button icon={<PlusOutlined aria-hidden="true" />} onClick={newSession}>新任务</Button>
        <Button type="text" icon={<HistoryOutlined aria-hidden="true" />} onClick={() => setRecent(true)}>最近任务</Button><Button ref={panelToggle} type="text" icon={<LayoutOutlined aria-hidden="true" />} aria-controls="agent-task-region" aria-expanded={wide ? panelVisible : taskDrawer} onClick={togglePanel}>任务详情</Button></nav></header>
    {health.data && !health.data.enabled && <Alert type="info" title={neutralAvailable ? '分析能力当前不可用，明确的本地操作仍可使用' : '执行服务当前不可用'} description={health.data.reason} />}
    {health.error && <Alert type="error" title={health.error.message} />}
    {error && <Alert type="error" title={error} closable onClose={() => setError('')} />}
    {notice && <Alert type="info" title={notice} closable onClose={() => setNotice('')} />}
    {offeredIntent && <Alert type="info" title={`已保留未发送的草稿，可切换为“${offeredIntent.title}”示例。`} closable onClose={() => setOfferedIntent(null)}
      action={<Button onClick={() => { writeGoal(offeredIntent.draft); setOfferedIntent(null); goalInput.current?.focus() }}>使用所选示例</Button>} />}
    {mismatch && <Alert type="error" title="会话与当前项目不一致，请重新选择会话" />}
    {source.error && <Alert type="error" title={`带入成果不可用：${source.error.message}`} />}
    {source.data && !sessionId && <Alert type="info" title="已核实带入成果，可继续处理或与其它资料比较" />}
    {health.data?.enabled && !connection.data?.available && <Alert type="info" title="自由分析需要连接主控模型；图片文字提取可直接使用"
      action={<Button onClick={() => setSettings(true)}>打开主控设置</Button>} />}
    <div className={`agent-layout${wide && panelVisible ? ' has-task-panel' : ''}`}>
      <section className={`agent-conversation${!projection.items.length && !active ? ' is-empty' : ''}`} aria-label="Agent 会话">
        <div className="agent-history" aria-label="会话记录" tabIndex={0} ref={scroll.scroller} onScroll={scroll.onScroll}><div className="agent-timeline" ref={scroll.content}>{projection.items.length ? projection.items.map(item => <div key={item.id} data-timeline-id={item.id}>
          {item.type === 'message' ? <AgentMessage item={item} taskId={sessionId || undefined} /> : item.type === 'activity' ? <ToolActivity item={item} taskId={sessionId || undefined} /> :
            <EventContent event={item.event} taskId={sessionId || undefined} answered={answered.has(String(item.event.payload.decision_id))}
              answerable={!controlling && active?.status === 'waiting_user' && item.event.run_id === active.id && !mismatch} reply={(id, revision, content) => { void reply(id, revision, content) }} />}
        </div>) :
          <div className="audit-welcome"><span className="audit-welcome-kicker">CLEAR AGENT</span><Typography.Title level={2}>你想完成什么？</Typography.Title>
            <Typography.Paragraph type="secondary">直接提问或添加文件，Agent 会选择工具完成工作。</Typography.Paragraph>
            <div className="audit-examples">{[
              ['问答与整理', '解释内容，概括重点', '解释这份资料的重点，并列出需要进一步核实的问题。'],
              ['文件处理', '清洗数据，生成文件', '检查这份表格的数据格式，保留编号前导零和金额精度，生成处理后的文件。'],
              ['台账核对', '比对金额与来源', '按供应商编号核对两份台账，列出金额差异及来源，再导出核对结果。'],
              ['取证线索', '整理观察与审计草稿', '检查这些图片的取证线索，整理观察事实、待补信息和有来源的审计草稿。'],
            ].map(([title, description, content]) => <button type="button" key={title} className="audit-example" onClick={() => writeGoal(content)}><strong>{title}</strong><span>{description}</span></button>)}</div>
          </div>}</div></div>
        <div className="agent-composer">
          {!scroll.following && <Button className="agent-return-latest" onClick={scroll.jumpToLatest}>回到最新消息{scroll.unread ? ` · ${scroll.unread} 项更新` : ''}</Button>}
          {projection.protocolIssue && <Alert type="warning" title={projection.protocolIssue} action={<Button size="small" onClick={reconnect}>重新核实订阅</Button>} />}
          <div className="agent-stage-bar">
            <RunStatus activity={activity} activeStatus={active?.status} taskStatus={task.data?.status} manual={task.data?.manual} submitting={send.isPending} controlling={controlling} subscription={subscription} reconnect={reconnect} job={workbenchView.currentJobs[0]} taskId={sessionId || undefined} />
            <button type="button" className="agent-context-trigger" aria-label="当前上下文" onClick={() => setDiagnostics(true)}>
              <span>运行详情</span>
              {compactionPending && <span>整理已安排</span>}
            </button>
          </div>
          <div className="agent-input-surface">
          {!!selectedAssets.length && <button type="button" className="agent-material-chip" onClick={() => { void openProjectAction('materials') }}><span className="agent-material-mark" aria-hidden="true" />{selectedAssets.length} 份资料<DownOutlined aria-hidden="true" /></button>}
          <Input.TextArea ref={goalInput} aria-label="任务或问题" value={goal} onChange={event => writeGoal(event.target.value)} autoSize={{ minRows: 2, maxRows: narrow ? 4 : 6 }}
            placeholder={active ? '补充条件、资料口径或下一步需求…' : '直接提问，或添加文件后说说你想完成什么…'} maxLength={16000}
            onCompositionStart={() => { composing.current = true }} onCompositionEnd={() => { composing.current = false }}
            onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !composing.current && !event.nativeEvent.isComposing && event.nativeEvent.keyCode !== 229) { event.preventDefault(); submitGoal() } }} />
          <div className="audit-composer-actions"><div className="agent-input-tools"><Button type="text" icon={<PlusOutlined aria-hidden="true" />} onClick={() => { void openProjectAction('materials') }} disabled={!neutralAvailable}>添加文件</Button>
            {(active || task.data) && <div className="agent-control-actions">
            {active?.status === 'needs_verification' && <Button disabled={controlling || mismatch} onClick={() => { void changeRun('resume') }}>核对任务并继续</Button>}
            {active?.status === 'paused' && <Button disabled={controlling || mismatch} onClick={() => { void changeRun('resume') }}>继续任务</Button>}
            {['failed', 'partial'].includes(active?.status || '') && active?.can_resume && <Button disabled={controlling || mismatch} onClick={() => { void changeRun('resume') }}>继续原任务</Button>}
            {active && ['running', 'waiting_job', 'waiting_user'].includes(active.status) && <Button type="text" disabled={controlling || mismatch} onClick={() => { void changeRun('pause') }}>暂停任务</Button>}
            {canStop && !task.data?.manual && <Button danger type="text" disabled={controlling || mismatch} title="停止后续步骤，并请求取消支持取消的本轮作业；已完成成果保留。" onClick={() => { void stop(true) }}>停止任务</Button>}
          </div>}
          <details ref={actionMenu} className="agent-action-menu" onKeyDown={event => { if (event.key === 'Escape' && actionMenu.current) { actionMenu.current.open = false; actionMenu.current.querySelector('summary')?.focus(); event.stopPropagation() } }}><summary>更多<DownOutlined aria-hidden="true" /></summary><div className="agent-menu-surface" role="group" aria-label="辅助任务操作">
            <span className="agent-menu-label">可选操作</span>
            {workspace && <Button type="text" onClick={() => { if (actionMenu.current) actionMenu.current.open = false; setDirectTools(true) }}>直接提取文字</Button>}
            <Button type="text" onClick={() => { if (actionMenu.current) actionMenu.current.open = false; setPlatformTools(true) }}>工具与参考设置</Button>
            <Button type="text" disabled={!workspace || mismatch} onClick={() => { if (actionMenu.current) actionMenu.current.open = false; setPreferences(true) }}>默认偏好</Button>
            <span className="agent-menu-label">任务与资料</span>
            {task.data && !task.data.manual && !pendingControl && <Button type="text" disabled={controlling || send.isPending || mismatch} onClick={() => { if (actionMenu.current) actionMenu.current.open = false; void control(true) }}>手动接管</Button>}
            <Button type="text" disabled={!goal.trim() || send.isPending || !!active || mismatch || !health.data?.enabled} onClick={() => send.mutate(captureSend(goal.trim(), false))}>保存目标</Button>
            {pendingControl ? <Button disabled={controlling || mismatch} onClick={() => { void control(pendingControl === 'take_over') }}>核实原{pendingControl === 'take_over' ? '接管' : '返回'}操作</Button> : task.data && (task.data.manual || hasPendingManualReturn(sessionId) ? <><Link to={manualHref}>打开手动工具</Link><ManualReturn compact taskId={task.data.id} workspaceId={task.data.workspace_id} /></> : null)}
            {sessionId && <Button loading={compact.isPending} disabled={controlling || mismatch || compactionPending || task.data?.manual} onClick={() => compact.mutate({ id: sessionId, scope: actionScope.current })}>{compactionPending ? '上下文整理已安排' : '整理上下文'}</Button>}
            <Button type="text" onClick={() => setDiagnostics(true)}>查看运行详情</Button>
            {canStop && <><span className="agent-menu-label">保留原生作业</span><Button disabled={controlling || mismatch} onClick={() => { void stop(false) }}>停止后续步骤</Button><small>保留已提交的原生作业与成果；主区“停止任务”会同时请求取消支持取消的作业。</small></>}
          </div></details></div>
            <Button className="agent-send" type="primary" icon={<ArrowUpOutlined aria-hidden="true" />} loading={send.isPending} disabled={sendDisabled}
              onClick={submitGoal}>{active ? '追加要求' : '发送'}</Button></div>
          </div>
          <div className="agent-composer-footnote">Enter 发送 · Shift+Enter 换行</div>
        </div>
      </section>
      {wide && <div className="agent-task-region" id="agent-task-region" inert={!panelVisible} aria-hidden={!panelVisible}>{panelVisible && <><div className="agent-task-region-heading"><span>任务进展</span><Button type="text" size="small" aria-label="收起任务详情" onClick={togglePanel}>收起</Button></div>{taskPanel}</>}</div>}
    </div>
    {workspace && <FileTextTools key={`${workspace}:${sessionId}`} open={directTools} close={() => setDirectTools(false)} workspace={workspace} sessionId={sessionId} selectedAssets={selectedAssets} scopeRevision={task.data?.scope_revision}
      selectFiles={() => { void openProjectAction('materials') }}
      disabled={!!active || controlling || send.isPending || mismatch || !neutralAvailable || !!sessionId && !task.data || !!task.data?.pending_scope}
      onAccepted={id => { setError(''); if (id !== sessionId) choose(id); refreshTask(); reconnect() }} />}
    <ControllerSettings open={settings} close={() => setSettings(false)} />
    <SkillsPanel open={skills} close={() => setSkills(false)} workspace={workspace} sessionId={sessionId} running={!!active && !task.data?.manual} />
    <AgentPreferences open={preferences} close={() => setPreferences(false)} workspace={workspace} sessionId={sessionId} />
    <PlatformTools open={platformTools} close={() => setPlatformTools(false)} workspace={workspace} scopeKey={`${workspace}:${sessionId}`} referenceIds={authorizedReferenceIds} saveReferences={saveReferences} deliveryPolicy={deliveryPolicy} policyResolution={task.data?.delivery_policy_resolution} saveDeliveryPolicy={saveDeliveryPolicy} pendingScope={!!task.data?.pending_scope} busy={send.isPending || controlling || mismatch || !!sessionId && !task.data} />
    <Materials open={materials} workspace={workspace} selected={selectedAssets} close={() => setMaterials(false)} save={saveScope} />
    <ProjectBackground open={background} profile={profile.data} close={() => setBackground(false)} save={async value => {
      const { revision, ...body } = value; await agentWrite(`/workspaces/${workspace}/audit-profile`, { ...body, expected_revision: revision, request_id: requestId() }, 'PUT'); void profile.refetch()
    }} />
    <Drawer title="最近任务" open={recent} onClose={() => setRecent(false)}>
      {!workspace && <>{projects.error ? <Alert type="error" title="暂时无法读取项目" action={<Button onClick={() => void projects.refetch()}>重试读取项目</Button>} /> : <Select aria-label="选择任务所在项目" placeholder={projects.isPending ? '正在读取项目…' : '选择项目'} loading={projects.isPending} showSearch optionFilterProp="label" style={{ width: '100%', marginBottom: 16 }}
        options={projects.data?.map(project => ({ value: project.workspace_id, label: project.name }))} onChange={id => { const next = new URLSearchParams(params); next.set('workspace', id); next.delete('session'); next.delete('continue'); clearSource(next); setKeyword(''); setParams(next) }} />}</>}
      <RecentTasks tasks={listing.data?.items || []} current={sessionId} choose={choose} keyword={keyword} search={setKeyword} hasProject={!!workspace}
        loading={health.isPending || listing.isFetching && !listing.data} error={listing.error || health.error} unavailable={!health.isPending && !neutralAvailable}
        retry={() => { void health.refetch(); if (workspace && neutralAvailable) void listing.refetch() }} />
    </Drawer>
    <Drawer title="任务详情" open={!wide && taskDrawer} onClose={() => setTaskDrawer(false)}>{!wide && taskDrawer && <div id="agent-task-region">{taskPanel}</div>}</Drawer>
    <Drawer title="运行详情" open={diagnostics} onClose={() => setDiagnostics(false)} size={480}>
      {diagnostics && <div className="agent-runtime-details">{latestBudget ? <ContextBudgetContent budget={latestBudget} /> : <Typography.Paragraph type="secondary">收到模型请求后显示上下文预算。</Typography.Paragraph>}
        {health.data?.engine && <Typography.Paragraph type="secondary">运行引擎：{health.data.engine}{health.data.engine_version ? ` ${health.data.engine_version}` : ''}</Typography.Paragraph>}
        {latestCompaction && <ContextCompactionContent payload={latestCompaction.payload} />}
      </div>}
    </Drawer>
  </div>
}
