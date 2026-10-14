import { memo } from 'react'
import { Alert, Tag, Typography } from 'antd'
import type { ActivityGroup, ToolStep } from './conversationProjection'
import { ContextBudgetContent, EnvironmentContent, ModelRequestContent, ModelUsageContent } from './ContextBudget'
import { ReferenceLinks, statusLabels } from './TaskPanel'
import type { ContextBudget } from './api'

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
export const ToolActivity = memo(function ToolActivity({ item, taskId }: { item: ActivityGroup; taskId?: string }) {
  const complete = item.tools.filter(tool => ['finished', 'restored'].includes(tool.phase) && tool.status === 'success').length
  const waiting = item.tools.filter(tool => ['started', 'restored', 'pending'].includes(tool.phase) && ['pending', 'running'].includes(tool.status)).length
  const failed = item.tools.filter(tool => tool.phase === 'blocked' || tool.status === 'error').length
  return <section className="agent-tool-activity" data-activity-id={item.id}>
    {!!item.tools.length && <><div className="agent-activity-overview">工具过程 · {item.tools.length} 项{complete ? `，${complete} 项完成` : ''}{waiting ? `，${waiting} 项执行或等待` : ''}{failed ? `，${failed} 项未完成` : ''}</div>
      <ol>{item.tools.map(tool => <li key={tool.id} data-call-id={tool.callId}><div className="agent-tool-heading">
        <span className="agent-tool-state"><Tag color={tool.status === 'error' || tool.phase === 'blocked' ? 'warning' : tool.status === 'success' ? 'success' : 'default'}>{stepLabel(tool)}</Tag></span>
        <div className="agent-tool-description"><Typography.Text>{tool.summary || tool.tool}</Typography.Text>
          {tool.summary && <span className="agent-tool-name">{tool.tool}</span>}</div>
        {recordedDuration(tool) && <span className="agent-tool-duration">用时 {recordedDuration(tool)}</span>}</div>
        <ReferenceLinks refs={tool.jobRefs} taskId={taskId} />
        <details className="agent-tool-details"><summary>调用详情</summary><Typography.Paragraph type="secondary">{tool.tool} · 操作 {tool.operationId || tool.callId}</Typography.Paragraph><pre>{JSON.stringify(tool.events.map(event => event.payload), null, 2)}</pre></details>
      </li>)}</ol>
    </>}
    {!!item.jobs.length && <div className="agent-job-progress" aria-label="原生作业进度">{item.jobs.map(job => <div className="agent-native-job" key={job.id}>
      <Typography.Text type={job.observation === 'unavailable' ? 'warning' : undefined}>{job.observation === 'unavailable' ? '进度暂不可读取' : job.stage || statusLabels[job.status] || job.status}</Typography.Text>
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
