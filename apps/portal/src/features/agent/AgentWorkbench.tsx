import { useEffect, useMemo, useRef, useState } from 'react'
import { Alert, Button, Card, Drawer, Input, Space, Tag, Typography } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { ControllerSettings } from './ControllerSettings'
import { ContextBudgetContent, EnvironmentContent, ModelRequestContent, ModelUsageContent } from './ContextBudget'
import { SkillsPanel } from './SkillsPanel'
import { AgentPreferences } from './AgentPreferences'
import { ManualReturn, hasPendingManualReturn } from './ManualReturn'
import { AgentRequestError, agentRequest, agentWrite, requestId, type AgentEvent, type AgentRun, type AgentSession, type Controller, type AgentRef, type AuditTask, type AuditProfile, type ContextBudget } from './api'
import { AgentMarkdown, AgentMessage } from './AgentMessage'
import { ToolActivity } from './ToolActivity'
import { RunStatus } from './RunStatus'
import { useAgentEvents } from './useAgentEvents'
import { useConversationScroll } from './useConversationScroll'
import { terminalActivity } from './conversationProjection'
import { Materials, ProjectBackground } from './Materials'
import { RecentTasks, TaskPanel, ReferenceLinks, statusLabels } from './TaskPanel'
import './agent.css'
import type { Workspace } from '../../../../../packages/contracts/types.generated'

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
  if (event.kind === 'question') return <Card size="small" title={String(payload.question)}>
    {(answered || !answerable) && <Typography.Paragraph type="secondary">{answered ? '已答复' : '此问题当前不可答复'}</Typography.Paragraph>}
    <Space wrap>{((payload.options || []) as string[]).map(option => <Button key={option}
      disabled={answered || !answerable}
      onClick={() => reply(String(payload.decision_id), Number(payload.revision), option)}>{option}</Button>)}</Space>
    <Space.Compact block style={{ marginTop: 12 }}><Input disabled={answered || !answerable} value={answer} onChange={event => setAnswer(event.target.value)} placeholder="补充你的选择" />
      <Button disabled={!answer.trim() || answered || !answerable} onClick={() => { reply(String(payload.decision_id), Number(payload.revision), answer); setAnswer('') }}>回答</Button></Space.Compact>
  </Card>
  if (event.kind === 'delivery') {
    const delivery = payload.delivery as { result_refs?: AgentRef[]; artifact_refs?: AgentRef[]; data?: { delivery?: DeliveredBundle[]; limitations?: string[] } } | null
    return <Card title={labels[String(payload.status)] || '成果'} size="small">
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
  const bundle = params.get('bundle'), sourceResult = params.get('result'), sourceSession = params.get('data_session')
  const assetSetSource = params.get('asset_set')
  const [settings, setSettings] = useState(false), [skills, setSkills] = useState(false), [goal, setGoal] = useState('')
  const [preferences, setPreferences] = useState(false)
  const [materials, setMaterials] = useState(false), [background, setBackground] = useState(false), [recent, setRecent] = useState(false), [keyword, setKeyword] = useState('')
  const [error, setError] = useState(''), [controlling, setControlling] = useState(false), [taskDrawer, setTaskDrawer] = useState(false)
  const [pendingControl, setPendingControl] = useState<'take_over' | 'return_to_agent' | null>(null)
  const [narrow, setNarrow] = useState(() => !!window.matchMedia?.('(max-width: 850px)').matches)
  const actionScope = useRef(`${workspace}:${sessionId}`); actionScope.current = `${workspace}:${sessionId}`
  const client = useQueryClient()
  const health = useQuery({ queryKey: ['agent-health'], queryFn: () => agentRequest<{ enabled: boolean; reason: string }>('/agent/health') })
  const { projection, events, subscription, reconnect } = useAgentEvents(workspace, sessionId, !!health.data?.enabled)
  const connection = useQuery({ queryKey: ['agent-connection'], enabled: health.data?.enabled,
    queryFn: () => agentRequest<Controller>('/agent/connection') })
  const source = useQuery({ queryKey: ['agent-source', workspace, bundle, sourceResult, sourceSession, assetSetSource],
    enabled: !!workspace && !!(bundle || assetSetSource || sourceResult && sourceSession) && !sessionId && health.data?.enabled,
    queryFn: () => agentWrite<{ reference: AgentRef }>(`/workspaces/${workspace}/agent/references`,
      assetSetSource ? { kind: 'asset_set', object_id: assetSetSource } : bundle ? { kind: 'artifact', object_id: bundle } : { kind: 'result', object_id: sourceResult, parent_id: sourceSession }) })
  const listing = useQuery({ queryKey: ['audit-tasks', workspace, keyword], enabled: !!workspace && health.data?.enabled,
    queryFn: () => agentRequest<{ items: AuditTask[] }>(`/workspaces/${workspace}/audit-tasks?keyword=${encodeURIComponent(keyword)}`), refetchInterval: 4000 })
  const task = useQuery({ queryKey: ['audit-task', sessionId], enabled: !!sessionId && health.data?.enabled,
    queryFn: () => agentRequest<AuditTask>(`/agent/sessions/${sessionId}/task`), refetchInterval: 1500 })
  const profile = useQuery({ queryKey: ['audit-profile', workspace], enabled: !!workspace && health.data?.enabled,
    queryFn: () => agentRequest<AuditProfile>(`/workspaces/${workspace}/audit-profile`) })
  const selectedAssets = task.data?.pending_scope ? (task.data.pending_scope as { selected_assets: string[] }).selected_assets : task.data?.selected_assets || draftAssets
  const session = useQuery({ queryKey: ['agent-session', sessionId], enabled: !!sessionId && health.data?.enabled,
    queryFn: () => agentRequest<AgentSession>(`/agent/sessions/${sessionId}`), refetchInterval: 1500 })
  const active = session.data?.runs?.find(run => ['running', 'waiting_job', 'waiting_user', 'needs_verification'].includes(run.status) && !terminalActivity(projection.activities[run.id]?.phase || ''))
  const mismatch = !!session.data && !!workspace && session.data.workspace_id !== workspace
  const answered = new Set(events.filter(event => event.kind === 'question_answered' || event.payload.decision_id && event.kind === 'message')
    .map(event => String(event.payload.decision_id)))
  const timelineIds = useMemo(() => projection.items.map(item => item.id), [projection.items])
  const scroll = useConversationScroll(`${workspace}:${sessionId}`, timelineIds, events.at(-1)?.seq || 0)
  const activity = active ? projection.activities[active.id] : Object.values(projection.activities).sort((a, b) => b.seq - a.seq)[0]
  const clearSource = (next: URLSearchParams) => { ['assets', 'bundle', 'result', 'data_session', 'asset_set'].forEach(key => next.delete(key)) }
  const choose = (id: string) => { const next = new URLSearchParams(params); next.set('session', id); clearSource(next); setGoal(''); setParams(next); setRecent(false) }
  const draftKey = `clear-audit-draft:${workspace || 'new'}:${sessionId || 'new'}`
  useEffect(() => {
    const draft = localStorage.getItem(draftKey)
    try { const value = draft ? JSON.parse(draft) : null; setGoal(value?.goal || ''); setDraftAssets(value?.assets || (params.get('assets') || '').split(',').filter(Boolean)) } catch { setGoal('') }
  }, [draftKey, params.get('continue')])
  const writeGoal = (value: string) => { setGoal(value); localStorage.setItem(draftKey, JSON.stringify({ goal: value, assets: selectedAssets })) }
  const createProject = async () => {
    if (workspace) return workspace
    const key = 'clear-audit-new-project'
    const attempt = JSON.parse(sessionStorage.getItem(key) || 'null') as { request_id: string } | null
    const command = attempt || { request_id: requestId() }; sessionStorage.setItem(key, JSON.stringify(command))
    const created = await agentWrite<Workspace>('/workspaces', { schema_version: '1.0', name: '智能审计项目', ...command })
    client.setQueryData<Workspace[]>(['workspaces'], previous => [...(previous || []).filter(item => item.workspace_id !== created.workspace_id), created])
    void client.invalidateQueries({ queryKey: ['workspaces'] }); return created.workspace_id
  }
  const openProjectAction = async (kind: 'materials' | 'background') => {
    try {
      if (kind === 'background') { setBackground(true); return }
      const project = await createProject()
      if (!workspace) { localStorage.setItem(`clear-audit-draft:${project}:new`, JSON.stringify({ goal, assets: selectedAssets })); const next = new URLSearchParams(params); next.set('workspace', project); setParams(next) }
      setMaterials(true)
    } catch (caught) { setError((caught as Error).message) }
  }
  const refreshTask = () => { if (sessionId) { void task.refetch(); void session.refetch() } void client.invalidateQueries({ queryKey: ['audit-tasks', workspace] }) }
  const saveScope = async (ids: string[]) => {
    if (sessionId && task.data) await agentWrite(`/agent/sessions/${sessionId}/task`, { request_id: requestId(), expected_revision: task.data.revision, selected_assets: ids }, 'PUT')
    setDraftAssets(ids); localStorage.setItem(draftKey, JSON.stringify({ goal, assets: ids })); refreshTask()
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
  useEffect(() => { setError(''); setControlling(false); setTaskDrawer(false)
    try { const pending = JSON.parse(sessionStorage.getItem(`clear-agent-control:${sessionId}`) || 'null') as { action?: string } | null
      setPendingControl(pending?.action === 'take_over' || pending?.action === 'return_to_agent' ? pending.action : null)
    } catch { setPendingControl(null) }
  }, [workspace, sessionId])
  const send = useMutation({ mutationFn: async ({ content, execute }: { content: string; execute: boolean }) => {
    if (mismatch) throw new Error('会话属于其他项目，请重新选择会话')
    if ((bundle || sourceResult || assetSetSource) && !sessionId && !source.data) throw new Error('先核实带入成果的来源和版本')
    const references = source.data ? [source.data.reference] : []
    const stamp = JSON.stringify([workspace, sessionId, selectedAssets, content, references])
    const stored = sessionStorage.getItem('clear-agent-pending-send')
    let attempt = stored ? JSON.parse(stored) as { stamp: string; content: string; project: string | null; conversation: string | null; projectRequest: string; sessionRequest: string; messageRequest: string } : null
    if (!attempt || attempt.stamp !== stamp) attempt = { stamp, content, project: workspace, conversation: sessionId,
      projectRequest: requestId(), sessionRequest: requestId(), messageRequest: requestId() }
    const retain = () => sessionStorage.setItem('clear-agent-pending-send', JSON.stringify(attempt))
    retain()
    let project = attempt.project, conversation = attempt.conversation
    if (!project) {
      project = await createProject()
      attempt.project = project; retain()
      void client.invalidateQueries({ queryKey: ['workspaces'] })
    }
    if (!conversation) {
      const created = await agentWrite<AgentSession>(`/workspaces/${project}/agent/sessions`,
        { name: content.slice(0, 60), selected_assets: selectedAssets, initial_refs: references, request_id: attempt.sessionRequest })
      conversation = created.id
      attempt.conversation = conversation; retain()
    }
    if (execute) await agentWrite<AgentRun>(`/agent/sessions/${conversation}/messages`, { content, request_id: attempt.messageRequest })
    else { const current = await agentRequest<AuditTask>(`/agent/sessions/${conversation}/task`)
      await agentWrite(`/agent/sessions/${conversation}/task`, { request_id: attempt.messageRequest, expected_revision: current.revision, goal: content }, 'PUT') }
    sessionStorage.removeItem('clear-agent-pending-send')
    return { project, conversation, execute, content }
  }, onSuccess: ({ project, conversation, execute, content }) => {
    localStorage.removeItem(draftKey); sessionStorage.removeItem('clear-audit-new-project')
    localStorage.setItem(`clear-audit-draft:${project}:${conversation}`, JSON.stringify({ goal: execute ? '' : content, assets: selectedAssets }))
    setGoal(execute ? '' : content); const next = new URLSearchParams(params); next.set('workspace', project); next.set('session', conversation); clearSource(next); setParams(next)
    void client.invalidateQueries({ queryKey: ['audit-tasks', project] }); void client.invalidateQueries({ queryKey: ['agent-session', conversation] }); void client.invalidateQueries({ queryKey: ['audit-task', conversation] })
  }, onError: caught => setError(caught.message) })
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
    if (!active || controlling || mismatch) return
    const scope = actionScope.current, id = active.id, conversation = sessionId
    setControlling(true)
    try { await agentWrite(`/agent/runs/${id}/cancel`, { cancel_owned_jobs: owned }) }
    catch (caught) { if (scope === actionScope.current) setError((caught as Error).message) }
    finally { void client.invalidateQueries({ queryKey: ['agent-session', conversation] }); void client.invalidateQueries({ queryKey: ['audit-task', conversation] }); if (scope === actionScope.current) setControlling(false) }
  }
  const resume = async () => {
    if (!active) return
    try { await agentWrite(`/agent/runs/${active.id}/resume`, {}); setError(''); void session.refetch() }
    catch (caught) { setError((caught as Error).message) }
  }
  const newSession = () => { const next = new URLSearchParams(params); next.delete('session'); clearSource(next); setGoal(''); setDraftAssets([]); setParams(next); setRecent(false) }
  useEffect(() => { if (!window.matchMedia) return; const media = window.matchMedia('(max-width: 850px)'), update = () => setNarrow(media.matches)
    update(); media.addEventListener?.('change', update); return () => media.removeEventListener?.('change', update)
  }, [])
  const taskPanel = <TaskPanel task={task.data} busy={send.isPending || controlling} hasProject={!!workspace}
    materials={() => { void openProjectAction('materials') }} background={() => { void openProjectAction('background') }}
    skills={() => setSkills(true)} rename={name => {
      if (task.data && name.trim()) void agentWrite(`/agent/sessions/${sessionId}/task`, { request_id: requestId(), expected_revision: task.data.revision, name: name.trim() }, 'PUT').then(refreshTask).catch(e => setError(e.message))
    }} />
  const manualParams = new URLSearchParams(params)
  manualParams.delete('session')
  manualParams.delete('continue')
  if (sessionId) manualParams.set('task_return', sessionId)
  if (selectedAssets.length) manualParams.set('assets', selectedAssets.join(','))
  const manualHref = `/apps${manualParams.size ? `?${manualParams}` : ''}`
  return <div className="agent-workbench">
    <header className="agent-heading"><div className="agent-heading-title"><span className="audit-eyebrow">CLEAR / AGENT WORKSPACE</span>
      <div className="agent-heading-identity"><Typography.Title level={2}>智能审计</Typography.Title>
        <Tag className="agent-model-state" title={connection.data?.model} color={connection.data?.available ? 'success' : 'default'}>{connection.data?.available ? connection.data.model : '模型待连接'}</Tag></div>
      {task.data?.title && <Typography.Text className="agent-current-task" type="secondary" title={task.data.title}>{task.data.title}</Typography.Text>}</div>
      <nav className="audit-context-bar" aria-label="审计任务操作"><Button onClick={newSession}>新任务</Button>
        <Button type="text" onClick={() => setRecent(true)}>最近任务</Button><Button type="text" aria-expanded={taskDrawer} onClick={() => setTaskDrawer(true)}>任务详情</Button><Button type="text" disabled={!workspace || !health.data?.enabled || mismatch} onClick={() => setPreferences(true)}>工作偏好</Button></nav></header>
    {health.data && !health.data.enabled && <Alert type="warning" title="Agent 当前不可用" description={health.data.reason} />}
    {health.error && <Alert type="error" title={health.error.message} />}
    {error && <Alert type="error" title={error} closable onClose={() => setError('')} />}
    {mismatch && <Alert type="error" title="会话与当前项目不一致，请重新选择会话" />}
    {source.error && <Alert type="error" title={`带入成果不可用：${source.error.message}`} />}
    {source.data && !sessionId && <Alert type="info" title="已核实带入成果，可继续处理或与其它资料比较" />}
    {health.data?.enabled && !connection.data?.available && <Alert type="info" title="先连接一个支持工具调用的主控模型"
      action={<Button onClick={() => setSettings(true)}>打开主控设置</Button>} />}
    <div className="agent-layout">
      <section className="agent-conversation" aria-label="Agent 会话">
        <div className="agent-history" ref={scroll.scroller} onScroll={scroll.onScroll}><div className="agent-timeline" ref={scroll.content}>{projection.items.length ? projection.items.map(item => <div key={item.id} data-timeline-id={item.id}>
          {item.type === 'message' ? <AgentMessage item={item} taskId={sessionId || undefined} /> : item.type === 'activity' ? <ToolActivity item={item} taskId={sessionId || undefined} /> :
            <EventContent event={item.event} taskId={sessionId || undefined} answered={answered.has(String(item.event.payload.decision_id))}
              answerable={!controlling && active?.status === 'waiting_user' && item.event.run_id === active.id && !mismatch} reply={(id, revision, content) => { void reply(id, revision, content) }} />}
        </div>) :
          <div className="audit-welcome"><span className="audit-welcome-kicker">目标 · 工具 · 成果</span><Typography.Title level={2}>从一个审计目标开始</Typography.Title>
            <Typography.Paragraph type="secondary">描述需求，添加资料。Agent 会选择工具、读取结果并交付成果。</Typography.Paragraph>
            <div className="audit-examples">{[
              ['台账核对', '比对金额与来源', '按供应商编号核对两份台账，列出金额差异及来源，再导出核对结果。'],
              ['重复与汇总', '保留来源行与统计', '检查台账中的重复候选，并按供应商汇总数量和金额，保留来源行。'],
              ['凭证与制度', '梳理资料与核对口径', '读取项目中的凭证和制度资料，解释核对口径；缺少结构化表格时先告诉我。'],
              ['取证线索', '整理观察与审计草稿', '检查这些图片的取证线索，整理观察事实、待补信息和有来源的审计草稿。'],
            ].map(([title, description, content]) => <button type="button" key={title} className="audit-example" onClick={() => writeGoal(content)}><strong>{title}</strong><span>{description}</span></button>)}</div>
          </div>}</div></div>
        <div className="agent-composer">
          {!scroll.following && <Button className="agent-return-latest" onClick={scroll.jumpToLatest}>回到最新消息{scroll.unread ? ` · ${scroll.unread} 项更新` : ''}</Button>}
          {projection.protocolIssue && <Alert type="warning" title={projection.protocolIssue} action={<Button size="small" onClick={reconnect}>重新核实订阅</Button>} />}
          <RunStatus activity={activity} activeStatus={active?.status} taskStatus={task.data?.status} manual={task.data?.manual} submitting={send.isPending} controlling={controlling} subscription={subscription} reconnect={reconnect} />
          {(active || task.data) && <Space wrap className="agent-control-actions">
            {active?.status === 'needs_verification' && <Button disabled={controlling || mismatch} onClick={() => { void resume() }}>核对任务并继续</Button>}
            {active && <><Button disabled={controlling || mismatch} onClick={() => { void stop(false) }}>停止后续步骤</Button>{narrow ? <details className="agent-cancel-options"><summary>取消选项</summary><Button disabled={controlling || mismatch} onClick={() => { void stop(true) }}>停止并取消支持取消的本轮任务</Button></details> : <Button disabled={controlling || mismatch} onClick={() => { void stop(true) }}>停止并取消支持取消的本轮任务</Button>}</>}
            {pendingControl ? <Button disabled={controlling || mismatch} onClick={() => { void control(pendingControl === 'take_over') }}>核实原{pendingControl === 'take_over' ? '接管' : '返回'}操作</Button> : task.data && (task.data.manual || hasPendingManualReturn(sessionId) ? <><Link to={manualHref}>打开手动工具</Link><ManualReturn compact taskId={task.data.id} workspaceId={task.data.workspace_id} /></> : <Button disabled={controlling || send.isPending || mismatch} onClick={() => { void control(true) }}>手动接管</Button>)}
          </Space>}
          {!!selectedAssets.length && <Tag>{selectedAssets.length} 份审计资料</Tag>}
          <Input.TextArea aria-label="审计目标或问题" value={goal} onChange={event => writeGoal(event.target.value)} autoSize={{ minRows: 2, maxRows: narrow ? 4 : 6 }}
            placeholder={active ? '补充条件、资料口径或下一步需求…' : '输入审计目标，或向大模型提问…'} maxLength={16000} />
          <div className="audit-composer-actions"><Space wrap><Button onClick={() => { void openProjectAction('materials') }} disabled={!health.data?.enabled}>添加资料</Button>
            <details className="agent-secondary-action"><summary>更多</summary><Button disabled={!goal.trim() || send.isPending || !!active || mismatch || !health.data?.enabled} onClick={() => send.mutate({ content: goal.trim(), execute: false })}>保存目标</Button></details></Space>
            <Button type="primary" loading={send.isPending} disabled={!goal.trim() || controlling || !connection.data?.available || task.data?.manual || mismatch || !!((bundle || sourceResult) && !sessionId && !source.data)}
              onClick={() => { setError(''); send.mutate({ content: goal.trim(), execute: true }) }}>{active ? '补充并继续' : '发送'}</Button></div>
        </div>
      </section>
    </div>
    <ControllerSettings open={settings} close={() => setSettings(false)} />
    <SkillsPanel open={skills} close={() => setSkills(false)} workspace={workspace} sessionId={sessionId} running={!!active && !task.data?.manual} />
    <AgentPreferences open={preferences} close={() => setPreferences(false)} workspace={workspace} sessionId={sessionId} />
    <Materials open={materials} workspace={workspace} selected={selectedAssets} close={() => setMaterials(false)} save={saveScope} />
    <ProjectBackground open={background} profile={profile.data} close={() => setBackground(false)} save={async value => {
      const { revision, ...body } = value; await agentWrite(`/workspaces/${workspace}/audit-profile`, { ...body, expected_revision: revision, request_id: requestId() }, 'PUT'); void profile.refetch()
    }} />
    <Drawer title="最近审计任务" open={recent} onClose={() => setRecent(false)}><RecentTasks tasks={listing.data?.items || []} current={sessionId} choose={choose} keyword={keyword} search={setKeyword} /></Drawer>
    <Drawer title="任务详情" open={taskDrawer} onClose={() => setTaskDrawer(false)}>{taskDrawer && taskPanel}</Drawer>
  </div>
}
