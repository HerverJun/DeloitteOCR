import { afterEach, beforeEach, expect, test, vi } from 'vitest'
import { act, cleanup, renderHook } from '@testing-library/react'
import { useAgentEvents } from '../src/features/agent/useAgentEvents'

class Source {
  static instances: Source[] = []
  readyState = 0; onopen: (() => void) | null = null; onerror: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  constructor(public url: string) { Source.instances.push(this) }
  close() { this.readyState = 2 }
}
const response = (body: unknown) => ({ ok: true, json: async () => body })
const replay = { items: [{ seq: 1, kind: 'message', run_id: null, created: '', payload: { role: 'user', content: '已保存的工作' } }], next_cursor: 1 }
const settle = async () => { await act(async () => { await Promise.resolve() }) }
beforeEach(() => { vi.useFakeTimers(); Source.instances = []; vi.stubGlobal('EventSource', Source) })
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals() })
test('an initial network failure backs off, reads saved events and never repeats a business write', async () => {
  let reads = 0
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => {
    expect(init?.method).toBeUndefined()
    if (path.endsWith('/jobs')) return response({ sampled_at: '', jobs: [] })
    if (++reads <= 2) throw new TypeError('Failed to fetch')
    return response(replay)
  })
  vi.stubGlobal('fetch', fetcher)
  const { result } = renderHook(() => useAgentEvents('workspace', 'session', true))
  await settle(); expect(result.current.subscription.status).toBe('reconnecting'); expect(reads).toBe(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(999) }); expect(reads).toBe(1)
  await act(async () => { await vi.advanceTimersByTimeAsync(1) }); expect(reads).toBe(2)
  await act(async () => { await vi.advanceTimersByTimeAsync(1999) }); expect(reads).toBe(2)
  await act(async () => { await vi.advanceTimersByTimeAsync(1) })
  expect(reads).toBe(3); expect(result.current.events.map(item => item.seq)).toEqual([1])
  expect(Source.instances).toHaveLength(1); expect(Source.instances[0].url).toContain('after=1')
})
test('online wakes an initial temporary server failure immediately and cleanup cancels later recovery', async () => {
  let online = false, reads = 0
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/jobs')) return response({ sampled_at: '', jobs: [] })
    reads++
    return online ? response(replay) : { ok: false, status: 503, json: async () => ({ detail: '暂时不可用' }) }
  }))
  const view = renderHook(() => useAgentEvents('workspace', 'session', true))
  await settle(); expect(reads).toBe(1)
  online = true; await act(async () => { window.dispatchEvent(new Event('online')) })
  expect(reads).toBe(2); expect(Source.instances).toHaveLength(1)
  const source = Source.instances[0]
  view.unmount(); expect(source.readyState).toBe(2)
  window.dispatchEvent(new Event('online'))
  await act(async () => { await vi.advanceTimersByTimeAsync(60000) }); expect(reads).toBe(2)
})
test.each([403, 404, 'invalid'])('a permanent %s replay error waits for an explicit reconnect instead of retrying indefinitely', async failure => {
  let reads = 0
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    if (path.endsWith('/jobs')) return response({ sampled_at: '', jobs: [] })
    reads++
    return failure === 'invalid' ? response({ items: [{}], next_cursor: 1 }) : { ok: false, status: failure, json: async () => ({ detail: '无法访问会话' }) }
  }))
  const { result } = renderHook(() => useAgentEvents('workspace', 'session', true))
  await settle(); expect(result.current.subscription.status).toBe('failed')
  window.dispatchEvent(new Event('online'))
  await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
  expect(reads).toBe(1); expect(Source.instances).toHaveLength(0)
  await act(async () => { result.current.reconnect() }); expect(reads).toBe(2)
})
test('leaving a failed session aborts its pending request and clears the scheduled reconnect', async () => {
  let reads = 0, signal: AbortSignal | null | undefined
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/jobs')) return response({ sampled_at: '', jobs: [] })
    reads++; signal = init?.signal; throw new TypeError('offline')
  }))
  const view = renderHook(() => useAgentEvents('workspace', 'session', true))
  await settle(); view.unmount()
  expect(signal?.aborted).toBe(true)
  await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
  expect(reads).toBe(1)
})
test('a permanently closed SSE waits for explicit reconnect because its HTTP permission or missing-route status is unavailable', async () => {
  const fetcher = vi.fn(async (path: string) => response(path.endsWith('/jobs') ? { sampled_at: '', jobs: [] } : replay))
  vi.stubGlobal('fetch', fetcher)
  const { result } = renderHook(() => useAgentEvents('workspace', 'session', true))
  await settle(); const source = Source.instances[0]
  act(() => { source.close(); source.onerror?.() })
  expect(result.current.subscription.status).toBe('failed')
  const requests = fetcher.mock.calls.length
  window.dispatchEvent(new Event('online'))
  await act(async () => { await vi.advanceTimersByTimeAsync(60000) })
  expect(Source.instances).toHaveLength(1); expect(fetcher).toHaveBeenCalledTimes(requests)
  await act(async () => { result.current.reconnect() })
  expect(Source.instances).toHaveLength(2)
})
