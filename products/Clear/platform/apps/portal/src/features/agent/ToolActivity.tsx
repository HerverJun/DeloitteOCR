import { memo, useCallback, useId, useState } from 'react'
import { Alert, Tag, Typography } from 'antd'
import type { ActivityGroup, ToolStep } from './conversationProjection'
import { ContextBudgetContent, EnvironmentContent, ModelRequestContent, ModelUsageContent } from './ContextBudget'
import { ReferenceLinks, statusLabels } from './TaskPanel'
import type { ContextBudget } from './api'
import { AnimatedDisclosure, useActivityListMotion } from './conversationMotion'
import './tool-activity.css'

const isComplete = (tool: ToolStep) => ['finished', 'restored'].includes(tool.phase) && tool.status === 'success'
const isIssue = (tool: ToolStep) => tool.phase === 'blocked' || ['error', 'failed', 'needs_verification'].includes(tool.status)
type ToolRun = { id: string; complete: boolean; tools: ToolStep[] }
export function groupToolSteps(tools: ToolStep[]): ToolRun[] {
  const groups: ToolRun[] = []
  for (const tool of tools) {
    const complete = isComplete(tool), previous = groups.at(-1)
    if (complete && previous?.complete) previous.tools.push(tool)
    else groups.push({ id: tool.id, complete, tools: [tool] })
  }
  return groups
}

function stepLabel(tool: ToolStep) {
  if (tool.phase === 'started') return '正在执行'
  if (tool.phase === 'restored') return tool.status === 'success' ? '已恢复 · 已完成' : tool.status === 'pending' ? '恢复原操作 · 等待作业' : `恢复原操作 · ${statusLabels[tool.status] || tool.status}`
  if (tool.phase === 'blocked') return '未执行'
  if (tool.phase === 'pending') return '等待作业'
  return statusLabels[tool.status] || ({ success: '已完成', error: '失败' } as Record<string, string>)[tool.status] || tool.status
}
function recordedDuration(tool: ToolStep) {
  const start = tool.events.find(event => event.payload.phase === 'started')
  const end = tool.events.find(event => event.payload.phase === 'finished' && ['success', 'error'].includes(String(event.payload.status)))
  if (!start || !end) return undefined
  const seconds = (Date.parse(end.created) - Date.parse(start.created)) / 1000
  if (!Number.isFinite(seconds) || seconds < 0) return undefined
  return seconds < 1 ? '不到 1 秒' : `${Math.floor(seconds)} 秒`
}
const nativePurposes: Record<string, string> = {
  read: '读取文件', read_image: '查看图片', glob: '查找文件', grep: '搜索文件内容',
  write: '写入文件', edit: '编辑文件', bash: '运行命令', powershell: '运行命令',
  web_fetch: '读取网页', web_search: '搜索网页', task: '执行子任务',
  task_list: '查看任务', task_read: '读取任务', task_output: '读取任务结果',
  wait_task: '等待任务', job_list: '查看作业', job_output: '读取作业结果',
  list_agents: '查看协作任务', skill: '读取操作指引',
}
function stepSummary(tool: ToolStep) {
  const summary = tool.summary.trim(), purpose = nativePurposes[tool.tool]
  const rawOutput = !!purpose && /[\r\n]|<\/?(?:path|type|content)\b|\b[A-Za-z]:[\\/]|(?:^|\s)\/(?:[^/\s]+\/)/i.test(summary)
  if (purpose && (!isIssue(tool) || !summary || rawOutput || summary === '工具开始执行')) {
    const path = summary.match(/<path>([^<\r\n]+)<\/path>/i)?.[1]
    const filename = path?.replace(/\\/g, '/').split('/').filter(Boolean).at(-1)
    const name = filename ? [...filename] : []
    return name.length ? `${purpose} · ${name.slice(0, 40).join('')}${name.length > 40 ? '…' : ''}` : purpose
  }
  if (!summary || rawOutput) return purpose || '工具操作'
  const readable = summary.replace(/\s+/g, ' '), characters = [...readable]
  return characters.length > 120 ? `${characters.slice(0, 120).join('')}…` : readable
}
const ToolRow = memo(function ToolRow({ tool, taskId, diagnosticsOpen, toggleDiagnostics }: {
  tool: ToolStep; taskId?: string; diagnosticsOpen: boolean; toggleDiagnostics: (toolId: string, open: boolean) => void
}) {
  const duration = recordedDuration(tool)
  return <li className={`agent-tool-row${isIssue(tool) ? ' is-issue' : ''}${isComplete(tool) ? ' is-complete' : ' is-current'}`} data-call-id={tool.callId} data-reading-anchor={tool.id}>
    <div className="agent-tool-heading">
      <span className="agent-tool-state"><Tag color={isIssue(tool) ? 'warning' : isComplete(tool) ? 'success' : 'default'}>{stepLabel(tool)}</Tag></span>
      <div className="agent-tool-description"><Typography.Text>{stepSummary(tool)}</Typography.Text></div>
      {duration && <span className="agent-tool-duration">用时 {duration}</span>}
    </div>
    <ReferenceLinks refs={tool.jobRefs} taskId={taskId} />
    <details className="agent-tool-details" open={diagnosticsOpen} onToggle={event => toggleDiagnostics(tool.id, event.currentTarget.open)}>
      <summary>调用详情</summary><Typography.Paragraph type="secondary">{tool.tool} · 操作 {tool.operationId || tool.callId}</Typography.Paragraph><pre>{JSON.stringify(tool.events.map(event => event.payload), null, 2)}</pre>
    </details>
  </li>
})
export const ToolActivity = memo(function ToolActivity({ item, taskId }: { item: ActivityGroup; taskId?: string }) {
  const [expandedTools, setExpandedTools] = useState(() => new Set<string>()), [diagnostics, setDiagnostics] = useState(() => new Set<string>())
  const disclosureId = useId()
  const toolList = useActivityListMotion(item.id, JSON.stringify(item.tools.map(tool => [tool.id, tool.phase, tool.status])))
  const toggleDiagnostics = useCallback((id: string, open: boolean) => {
    setDiagnostics(previous => { if (previous.has(id) === open) return previous; const next = new Set(previous); if (open) next.add(id); else next.delete(id); return next })
    if (open) setExpandedTools(previous => previous.has(id) ? previous : new Set([...previous, id]))
  }, [])
  const complete = item.tools.filter(isComplete).length
  const waiting = item.tools.filter(tool => ['started', 'restored', 'pending'].includes(tool.phase) && ['pending', 'running'].includes(tool.status)).length
  const failed = item.tools.filter(isIssue).length
  return <section className="agent-tool-activity" data-activity-id={item.id}>
    {!!item.tools.length && <><div className="agent-activity-overview">工具过程 · {item.tools.length} 项{complete ? `，${complete} 项完成` : ''}{waiting ? `，${waiting} 项执行或等待` : ''}{failed ? `，${failed} 项未完成` : ''}</div>
      <ol ref={toolList} className="agent-tool-runs">{groupToolSteps(item.tools).map(run => {
        if (!run.complete) return <ToolRow key={run.id} tool={run.tools[0]} taskId={taskId} diagnosticsOpen={diagnostics.has(run.id)} toggleDiagnostics={toggleDiagnostics} />
        const open = run.tools.some(tool => expandedTools.has(tool.id)), panelId = `${disclosureId}-${run.id}`
        return <li className="agent-tool-completed-run" key={run.id} data-completed-run={run.id}>
          <button className="agent-tool-run-toggle" type="button" aria-expanded={open} aria-controls={panelId} onClick={() => setExpandedTools(previous => {
            const next = new Set(previous); for (const tool of run.tools) { if (open) next.delete(tool.id); else next.add(tool.id) }; return next
          })}><span className="agent-tool-run-check" aria-hidden="true">✓</span><span>已完成 {run.tools.length} 项</span><span className="agent-tool-run-preview">{!open && stepSummary(run.tools.at(-1)!)}</span><span className="agent-tool-run-chevron" aria-hidden="true">›</span></button>
          <AnimatedDisclosure id={panelId} open={open}><ol>{run.tools.map(tool => <ToolRow key={tool.id} tool={tool} taskId={taskId} diagnosticsOpen={diagnostics.has(tool.id)} toggleDiagnostics={toggleDiagnostics} />)}</ol></AnimatedDisclosure>
        </li>
      })}</ol>
    </>}
    {!!item.jobs.length && <div className="agent-job-progress" aria-label="原生作业进度">{item.jobs.map(job => <div className="agent-native-job" key={job.id} data-reading-anchor={`job:${job.id}`}>
      <Typography.Text type={job.observation === 'unavailable' ? 'warning' : undefined}>{job.observation === 'unavailable' ? '状态暂不可核实' : job.stage || statusLabels[job.status] || job.status}</Typography.Text>
      {job.observation !== 'unavailable' && typeof job.processed === 'number' && <Typography.Text> · 已处理 {job.processed}{typeof job.total === 'number' && job.total > 0 ? ` / ${job.total}` : ''}</Typography.Text>}
      {job.observation !== 'unavailable' && job.counts && Object.keys(job.counts).length > 0 && <Typography.Text type="secondary"> · {Object.entries(job.counts).filter(([, count]) => typeof count === 'number').map(([name, count]) => `${name} ${count}`).join('，')}</Typography.Text>}
      {job.sampledAt && <Typography.Text type="secondary"> · {job.observation === 'unavailable' ? '尝试读取于' : '更新于'} {new Date(job.sampledAt).toLocaleTimeString('zh-CN', { hour12: false })}</Typography.Text>}
      {job.needsResume && <Alert type="info" title={job.resumeMessage || '原生作业已暂停，请在手动工具中恢复；Agent 保持等待。'} />}
      <ReferenceLinks refs={[job.jobRef]} taskId={taskId} />
      {(job.observation || job.availability || job.hasError !== undefined) && <details><summary>进度读取详情</summary><pre>{JSON.stringify({ observation: job.observation, availability: job.availability, has_error: job.hasError }, null, 2)}</pre></details>}
    </div>)}</div>}
    {!!item.diagnostics.length && <details className="agent-diagnostics"><summary>诊断 · 请求、上下文与用量</summary>{item.diagnostics.map(event => <div key={event.seq}>
      {event.kind === 'model_request' ? <ModelRequestContent payload={event.payload} /> : event.kind === 'usage' ? <ModelUsageContent payload={event.payload} /> : event.kind === 'context_injection' ? <EnvironmentContent payload={event.payload} /> : event.kind === 'model_transport' ?
        <Typography.Paragraph type="secondary">实际响应：{event.payload.mode === 'sse' ? 'SSE 流式' : event.payload.mode === 'json' ? '完整 JSON 结果' : '未请求流式'}；配置流式：{event.payload.requested ? '开启' : '关闭'}</Typography.Paragraph> :
        <Typography.Paragraph type="secondary">{event.kind}</Typography.Paragraph>}
      {!!event.payload.context_budget && event.kind !== 'model_request' && <ContextBudgetContent budget={event.payload.context_budget as ContextBudget} />}
    </div>)}</details>}
    {!!failed && <Alert type="warning" title={`${failed} 项工具操作未完成，可展开查看原因。`} />}
  </section>
})
