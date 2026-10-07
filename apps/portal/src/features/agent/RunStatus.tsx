import { useEffect, useState } from 'react'
import { Button, Space, Typography } from 'antd'
import type { RunActivity } from './conversationProjection'
import type { SubscriptionStatus } from './useAgentEvents'
import { statusLabels } from './TaskPanel'

const phases: Record<string, string> = { preparing_request: '正在准备请求', model_wait: '正在等待模型', receiving_response: '正在接收回应', validating_calls: '正在核实工具调用', tool_dispatch: '正在执行工具', waiting_job: '等待原生作业', waiting_user: '等待你补充信息', needs_verification: '原操作回执待核实', finalizing: '正在整理交付', completed: '本轮已结束', stopped: '已停止后续步骤', failed: '本轮未完成' }
const moving = new Set(['preparing_request', 'model_wait', 'receiving_response', 'validating_calls', 'tool_dispatch', 'waiting_job', 'finalizing'])
export function useReducedMotion() {
  const [reduced, setReduced] = useState(() => typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches)
  useEffect(() => { if (!window.matchMedia) return; const media = window.matchMedia('(prefers-reduced-motion: reduce)'), changed = () => setReduced(media.matches)
    changed(); media.addEventListener?.('change', changed); return () => media.removeEventListener?.('change', changed)
  }, [])
  return reduced
}
export function RunStatus({ activity, activeStatus, taskStatus, manual, submitting, subscription, reconnect, controlling = false }: {
  activity?: RunActivity; activeStatus?: string; taskStatus?: string; manual?: boolean; submitting: boolean; subscription: { status: SubscriptionStatus; error: string }; reconnect: () => void; controlling?: boolean
}) {
  const reduced = useReducedMotion(), [now, setNow] = useState(Date.now), [sentAt, setSentAt] = useState<number>()
  useEffect(() => { if (submitting || controlling) setSentAt(Date.now()); else setSentAt(undefined) }, [submitting, controlling])
  const phase = controlling ? 'control' : submitting ? 'submitting' : manual ? 'manual' : activity?.phase || activeStatus || taskStatus || ''
  const animated = !reduced && (phase === 'submitting' || phase === 'control' || moving.has(phase) || phase === 'running')
  const progressing = phase === 'submitting' || phase === 'control' || moving.has(phase) || phase === 'running'
  useEffect(() => { if (!progressing) return; setNow(Date.now()); const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer) }, [progressing, activity?.created])
  const started = submitting || controlling ? sentAt : activity?.created ? Date.parse(activity.created) : undefined
  const elapsed = typeof started === 'number' && Number.isFinite(started) ? Math.max(0, Math.floor((now - started) / 1000)) : undefined
  const label = phase === 'submitting' ? '正在提交' : phase === 'control' ? '正在交接控制' : phase === 'manual' ? '人工处理中' : phases[phase] || statusLabels[phase] || (phase ? 'Agent 处理中' : '')
  return <div className="agent-run-status" data-phase={phase} data-reduced-motion={reduced} data-animated={animated}>
    {label && <Space className="agent-phase-line"><span className={`agent-status-mark ${animated ? 'is-moving' : ''}`} aria-hidden="true"><i /><i /><i /></span><Typography.Text role="status" aria-live="polite">{label}</Typography.Text>
      {progressing && elapsed !== undefined && <Typography.Text type="secondary" aria-label="当前阶段耗时">{elapsed} 秒</Typography.Text>}</Space>}
    {subscription.status === 'loading' && <Typography.Text type="secondary">正在读取会话…</Typography.Text>}
    {subscription.status === 'reconnecting' && <Typography.Text type="secondary" role="status">正在恢复订阅，已有内容保留。</Typography.Text>}
    {subscription.status === 'failed' && <Space wrap><Typography.Text type="warning">{subscription.error || '订阅暂不可用'}</Typography.Text><Button size="small" onClick={reconnect}>重新连接订阅</Button></Space>}
  </div>
}
