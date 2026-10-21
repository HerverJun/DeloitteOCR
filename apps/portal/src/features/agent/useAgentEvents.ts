import { useEffect, useRef, useState } from 'react'
import { agentBase, agentRequest, type AgentEvent } from './api'
import { applyConversationEvents, applyJobSnapshot, createProjection, terminalActivity, validJobSnapshot, type ConversationProjection } from './conversationProjection'

export type SubscriptionStatus = 'idle' | 'loading' | 'connected' | 'reconnecting' | 'failed'
export function validAgentEvent(value: unknown): value is AgentEvent {
  if (!value || typeof value !== 'object') return false
  const event = value as AgentEvent
  return Number.isInteger(event.seq) && event.seq > 0 && typeof event.kind === 'string' && typeof event.created === 'string' &&
    (event.run_id === null || typeof event.run_id === 'string') && !!event.payload && typeof event.payload === 'object' && !Array.isArray(event.payload)
}
const urgent = (event: AgentEvent) => ['question', 'error', 'cancelled', 'task_control', 'response_state', 'context_compaction', 'delivery_check', 'job_progress', 'delivery'].includes(event.kind) || event.kind === 'agent_activity' && (terminalActivity(String(event.payload.phase)) || event.payload.phase === 'paused')

export function useAgentEvents(workspace: string | null, sessionId: string | null, enabled: boolean) {
  const key = `${workspace || ''}:${sessionId || ''}`
  const cache = useRef(new Map<string, ConversationProjection>())
  const currentKey = useRef(key)
  currentKey.current = key
  const [state, setState] = useState({ key, projection: createProjection() })
  const [connection, setConnection] = useState<{ key: string; status: SubscriptionStatus; error: string }>({ key, status: 'idle', error: '' })
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    let stopped = false, source: EventSource | undefined, batchTimer: ReturnType<typeof setTimeout> | undefined, failureTimer: ReturnType<typeof setTimeout> | undefined, snapshotTimer: ReturnType<typeof setTimeout> | undefined, catchingUp = false, readingJobs = false
    const abort = new AbortController(), pending = new Map<number, AgentEvent>()
    const cached = cache.current.get(key)
    let projection = cached?.protocolIssue ? createProjection() : cached || createProjection()
    // Only a complete HTTP replay page advances recovery. An out-of-order SSE
    // frame must not skip an earlier, undelivered event on the next reconnect.
    let cursor = cached?.protocolIssue ? 0 : projection.replayCursor || 0
    setState({ key, projection: cached || projection })
    const live = () => !stopped && currentKey.current === key
    const status = (status: SubscriptionStatus, error = '') => { if (live()) setConnection({ key, status, error }) }
    const publish = () => { cache.current.set(key, projection); if (live()) setState({ key, projection }) }
    const flush = () => {
      if (batchTimer) clearTimeout(batchTimer); batchTimer = undefined
      if (!live() || !pending.size) return
      projection = applyConversationEvents(projection, [...pending.values()]); pending.clear()
      publish()
    }
    const receive = (event: AgentEvent) => {
      if (!live() || projection.seen.has(event.seq) || pending.has(event.seq)) return
      pending.set(event.seq, event)
      if (urgent(event)) flush(); else batchTimer ||= setTimeout(flush, 125)
    }
    const catchUp = async () => {
      if (catchingUp || !live()) return
      catchingUp = true
      try {
        let page: { items: unknown[]; next_cursor: number }
        do {
          page = await agentRequest(`/agent/sessions/${encodeURIComponent(sessionId!)}/events?after=${cursor}&limit=100`, { signal: abort.signal })
          if (!live()) return
          if (!Array.isArray(page.items) || !Number.isInteger(page.next_cursor) || page.items.some(value => !validAgentEvent(value))) throw new Error('会话事件格式不可用，请重新连接订阅。')
          for (const event of page.items as AgentEvent[]) receive(event)
          cursor = Math.max(cursor, page.next_cursor)
        } while (page.items.length === 100)
        flush()
        projection = { ...projection, replayCursor: cursor }; publish()
      } finally { catchingUp = false }
    }
    const readJobs = async () => {
      if (readingJobs || !live()) return
      readingJobs = true
      try {
        const snapshot: unknown = await agentRequest(`/agent/sessions/${encodeURIComponent(sessionId!)}/jobs`, { signal: abort.signal })
        if (!live()) return
        if (!validJobSnapshot(snapshot)) throw new Error('作业快照不可读')
        flush(); projection = applyJobSnapshot(projection, snapshot); publish()
      } catch {
        if (live()) { flush(); projection = applyJobSnapshot(projection); publish() }
      } finally { readingJobs = false }
    }
    const recover = async () => {
      try { await catchUp() } finally { await readJobs() }
    }
    const pollJobs = async () => {
      await readJobs()
      if (live()) snapshotTimer = setTimeout(pollJobs, projection.jobVerification === 'unavailable' ? 5000 : 1500)
    }
    const connect = async () => {
      if (!enabled || !sessionId || !workspace) {
        if (!enabled && sessionId && workspace) { projection = applyJobSnapshot(projection); publish() }
        status('idle'); return
      }
      status('loading')
      try {
        await recover()
        if (!live()) return
        snapshotTimer = setTimeout(pollJobs, 1500)
        source = new EventSource(`${agentBase}/agent/sessions/${encodeURIComponent(sessionId)}/stream?after=${cursor}`)
        source.onopen = () => {
          if (!live()) return
          if (failureTimer) clearTimeout(failureTimer)
          status('connected')
          void recover().catch(caught => { if (live()) status('failed', (caught as Error).message) })
        }
        source.onmessage = message => {
          if (!live()) return
          try {
            const value: unknown = JSON.parse(message.data)
            if (!validAgentEvent(value)) throw new Error('会话事件格式不可用，请重新连接订阅。')
            receive(value)
          } catch { status('failed', '收到不可读的会话事件；已保留当前内容，请重新连接订阅。') }
        }
        source.onerror = () => {
          if (!live()) return
          flush()
          projection = applyJobSnapshot(projection); publish()
          status(source?.readyState === 2 ? 'failed' : 'reconnecting', '连接中断，正在恢复读取；模型与工具不会重发。')
          void recover().catch(() => { /* Job snapshot independently reports unverified state. */ })
          if (failureTimer) clearTimeout(failureTimer)
          failureTimer = setTimeout(() => status('failed', '订阅暂未恢复，可重新连接；已有内容保留。'), 15000)
        }
      } catch (caught) { if (live() && !(caught instanceof DOMException && caught.name === 'AbortError')) status('failed', (caught as Error).message) }
    }
    void connect()
    return () => { flush(); stopped = true; abort.abort(); source?.close(); if (batchTimer) clearTimeout(batchTimer); if (failureTimer) clearTimeout(failureTimer); if (snapshotTimer) clearTimeout(snapshotTimer) }
  }, [key, workspace, sessionId, enabled, retry])
  const projection = state.key === key ? state.projection : cache.current.get(key) || createProjection()
  const subscription = connection.key === key ? connection : { key, status: 'loading' as SubscriptionStatus, error: '' }
  return { projection, events: projection.events, subscription, reconnect: () => setRetry(value => value + 1) }
}
