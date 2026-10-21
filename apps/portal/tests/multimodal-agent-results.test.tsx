import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { ResultView, MultimodalWorkbench } from '../src/features/multimodal/MultimodalWorkbench'

vi.mock('../src/App', () => ({ useWorkspaceId: () => 'workspace', Workspaces: () => null }))
beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn(), addListener: vi.fn(), removeListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear() })
const seal = { job_id: 'job', kind: 'seal' as const, status: 'succeeded', stage: 'completed', revision: 1, error: null, result_hash: 'a'.repeat(64),
  inputs: [{ original_name: '合同.png', asset_id: 'asset', asset_revision: 1 }], created_at: '', review: null,
  result: { outcome: 'recognized', algorithm: '印章识别', device: 'cpu', elapsed_seconds: 1, limitations: ['候选需人工核对'], seals: [
    { row_id: 'row1', review_index: 0, page_number: 1, text: '甲公司', subject: '甲公司', number: '001', subject_check: 'not_requested' },
  ] } }
const response = (value: unknown) => ({ ok: true, json: async () => value })
function mount(element: React.ReactNode, url = '/apps/ocr/seals?workspace=workspace') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  const router = createMemoryRouter([{ path: '*', element }], { initialEntries: [url] })
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
  return router
}
const view = (readOnly = false) => <ResultView item={seal} workspaceId="workspace" refresh={() => {}} referenceName="" setReferenceName={() => {}} onReference={() => {}} referenceBusy={false} readOnly={readOnly} snapshotId={readOnly ? 'snapshot' : undefined} />

test('fixed snapshots are readonly, ignore unsaved local edits and download their pinned files', () => {
  localStorage.setItem('clear-manual-review:workspace:job:1', JSON.stringify({ edits: [{ row_id: 'row1', index: 0, text: '未保存内容' }] }))
  mount(view(true))
  expect(screen.getByLabelText('印章1text')).toHaveValue('甲公司'); expect(screen.getByLabelText('印章1text')).toHaveAttribute('readonly')
  expect(screen.queryByRole('button', { name: '保存复核与校正' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '候选字段转入草稿比对（未复核）' })).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: '下载此固定版本' })).toHaveAttribute('href', '/api/platform/v1/workspaces/workspace/multimodal/jobs/job/snapshots/snapshot/download')
})

test('unreviewed seal candidates may form a draft while formal conversion remains gated and lost responses reuse request identity', async () => {
  const writes: { path: string; body: { request_id: string; expected_revision: number } }[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    writes.push({ path, body: JSON.parse(String(init?.body)) }); throw new Error('回包丢失')
  }))
  mount(view())
  expect(screen.getByRole('button', { name: '将人工核准字段转入 ClearData' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '候选字段转入草稿比对（未复核）' }))
  await screen.findByText('回包丢失'); cleanup(); mount(view())
  fireEvent.click(screen.getByRole('button', { name: '候选字段转入草稿比对（未复核）' }))
  await waitFor(() => expect(writes).toHaveLength(2))
  expect(writes[0].path).toMatch(/\/draft-to-data$/); expect(writes[1].body).toEqual(writes[0].body)
  expect(writes[0].body.expected_revision).toBe(1); expect(writes[0].body.request_id).toMatch(/^r/)
})

test('snapshot page reads the pinned endpoint and blocks display on a citation hash mismatch', async () => {
  const paths: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    paths.push(path)
    return response(path.includes('/snapshots/') ? { snapshot_id: 'snapshot', revision: 1, review_revision: 0, review_state: 'unreviewed', snapshot_hash: 'b'.repeat(64), analysis: seal } : { items: [], total: 0, next_offset: null })
  }))
  mount(<MultimodalWorkbench kind="seal" />, `/apps/ocr/seals?workspace=workspace&analysis=job&snapshot=snapshot&sha256=${'a'.repeat(64)}&revision=1`)
  await screen.findByText('结果身份核验不一致')
  expect(paths.some(path => path.endsWith('/jobs/job/snapshots/snapshot'))).toBe(true)
  expect(paths.some(path => path.endsWith('/jobs/job'))).toBe(false)
  expect(screen.queryByLabelText('印章1text')).not.toBeInTheDocument()
})
