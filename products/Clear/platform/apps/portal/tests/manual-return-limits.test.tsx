import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ManualReturn } from '../src/features/agent/ManualReturn'

beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })

async function chooseResult(existing: number, additions: number) {
  const selected = Array.from({ length: existing }, (_, index) => `existing${index}`)
  const required = Array.from({ length: additions }, (_, index) => ({ asset_id: `new${index}`, name: `新资料${index}` }))
  const commands: { include_asset_ids: string[] }[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/manual-return')) { commands.push(JSON.parse(String(init?.body))); return { ok: true, json: async () => ({}) } }
    if (url.includes('/manual-results')) return { ok: true, json: async () => ({ items: [{ title: '人工结果', reference: { kind: 'result', application: 'ocr', workspace_id: 'w', object_id: 'result', revision: 1 }, required_assets: required }], unavailable: [], next_offset: null }) }
    return { ok: true, json: async () => ({ id: 'task', workspace_id: 'w', title: '任务', manual: true, revision: 1, selected_assets: selected }) }
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const router = createMemoryRouter([{ path: '*', element: <ManualReturn taskId="task" workspaceId="w" /> }], { initialEntries: ['/'] })
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
  fireEvent.click(await screen.findByRole('button', { name: '带回结果' }))
  fireEvent.click(await screen.findByRole('checkbox', { name: /人工结果/ }))
  return { commands, submit: screen.getByRole('button', { name: '返回 Agent' }) }
}

test.each([[200, 2], [900, 100], [999, 1]])('a task with %s existing assets accepts %s additions within both independent limits', async (existing, additions) => {
  const { commands, submit } = await chooseResult(existing, additions)
  expect(submit).toBeEnabled()
  await waitFor(() => expect(screen.getByText(`本次随结果加入 ${additions} 份资料，单次最多 100 份。合并后任务范围 ${existing + additions} 份，最多 1000 份。`)).toBeVisible())
  fireEvent.click(submit)
  await waitFor(() => expect(commands).toHaveLength(1))
  expect(commands[0].include_asset_ids).toHaveLength(additions)
  await waitFor(() => expect(JSON.parse(localStorage.getItem('clear-audit-draft:w:task')!).assets).toHaveLength(existing + additions))
})

test('more than 100 included assets are blocked even when the merged task scope is far below 1000', async () => {
  const { commands, submit } = await chooseResult(1, 101)
  expect(submit).toBeDisabled()
  await waitFor(() => expect(screen.getByText('本次结果需加入 101 份资料，超过单次带回的 100 份上限。请减少所选结果，分次带回。')).toBeVisible())
  fireEvent.click(submit)
  expect(commands).toHaveLength(0)
  expect(sessionStorage.getItem('clear-manual-return:task')).toBeNull()
})

test('a merged scope above 1000 is blocked even when this return includes only one asset', async () => {
  const { commands, submit } = await chooseResult(1000, 1)
  expect(submit).toBeDisabled()
  await waitFor(() => expect(screen.getByText('合并后任务范围为 1001 份资料，超过 1000 份上限。请减少所选结果或调整任务资料范围。')).toBeVisible())
  fireEvent.click(submit)
  expect(commands).toHaveLength(0)
})

test('a task from another project cannot expose a usable return action', async () => {
  const writes: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method) writes.push(url)
    return { ok: true, json: async () => ({ id: 'task', workspace_id: 'other', title: '其他项目任务', manual: true, revision: 1, selected_assets: [] }) }
  }))
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '*', element: <ManualReturn taskId="task" workspaceId="w" /> }], { initialEntries: ['/'] })
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
  expect(await screen.findByText('当前项目与关联任务不一致，请返回原对话核对。')).toBeVisible()
  const bringBack = screen.getByRole('button', { name: '带回结果' })
  expect(bringBack).toBeDisabled()
  fireEvent.click(bringBack)
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  expect(writes).toEqual([])
})
