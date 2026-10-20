import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { resolveTaskReturn } from './platformTaskReturn'

const scope = { app_id: 'ocr', instance_id: 'native1', workspace_id: 'workspace1' }
const base = 'http://127.0.0.1:8970/apps/ocr/start?workspace=workspace1'
let entries: Map<string, string>
beforeEach(() => {
  entries = new Map()
  vi.stubGlobal('location', { hash: '#platform_task_return=session1&platform_task_workspace=workspace1', pathname: '/', search: '' })
  vi.stubGlobal('history', { state: null, replaceState: vi.fn((_state, _title, href: string) => { location.hash = new URL(href, 'http://127.0.0.1:9000').hash }) })
  vi.stubGlobal('sessionStorage', { getItem: (key: string) => entries.get(key), setItem: (key: string, value: string) => entries.set(key, value), removeItem: (key: string) => entries.delete(key) })
})
afterEach(() => vi.unstubAllGlobals())

it('returns to the exact conversation and restores it after reload without retaining the hash', () => {
  const expected = 'http://127.0.0.1:8970/agent?workspace=workspace1&session=session1'
  expect(resolveTaskReturn(base, scope)).toBe(expected)
  expect(location.hash).toBe('')
  expect(resolveTaskReturn(base, scope)).toBe(expected)
})

it('rejects a different project and does not reuse another native instance', () => {
  expect(resolveTaskReturn(base, scope)).toContain('session=session1')
  expect(resolveTaskReturn(base, { ...scope, workspace_id: 'workspace2' })).toBeNull()
  expect(resolveTaskReturn(base, { ...scope, instance_id: 'otherNative' })).toBeNull()
})

it('rejects duplicate IDs and discards a previously stored return', () => {
  resolveTaskReturn(base, scope)
  location.hash = '#platform_task_return=session1&platform_task_return=session2&platform_task_workspace=workspace1'
  expect(resolveTaskReturn(base, scope)).toBeNull()
  expect(resolveTaskReturn(base, scope)).toBeNull()
})

it('storage failures leave the current verified navigation usable', () => {
  vi.stubGlobal('sessionStorage', { getItem() { throw new Error('Disabled') }, setItem() { throw new Error('Disabled') }, removeItem() {} })
  expect(resolveTaskReturn(base, scope)).toContain('session=session1')
})
