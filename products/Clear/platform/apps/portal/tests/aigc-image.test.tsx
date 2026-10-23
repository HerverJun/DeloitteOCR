import '@testing-library/jest-dom/vitest'
import { randomUUID } from 'node:crypto'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { AigcResultView, type AigcImageResult } from '../src/features/multimodal/AigcResultView'
import { MultimodalApplicationCard, MultimodalWorkbench, ResultView } from '../src/features/multimodal/MultimodalWorkbench'
import { referencePath } from '../src/features/agent/referencePath'
import type { AgentRef } from '../src/features/agent/api'

vi.mock('../src/App', () => ({ useWorkspaceId: () => 'workspace', Workspaces: () => null }))
beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => {
  vi.stubGlobal('crypto', { randomUUID })
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear() })
const raw: AigcImageResult = { schema_version: '1.0', state: 'unable_to_assess', raw_score: .823456789,
  score_direction: 'higher_is_generated', threshold: null, calibration: null, qualification: 'experimental',
  scope: ['通用图片待独立验收', '文档待独立验收'], limitations: ['截图与压缩会影响信号'], preprocessing_version: 'commfor-384-v1' }
const analysis = { job_id: 'job', kind: 'aigc_image' as const, status: 'succeeded', stage: 'completed', revision: 1,
  error: null, result_hash: 'a'.repeat(64), inputs: [{ original_name: '公开文档.png', asset_id: 'asset', asset_revision: 2 }],
  result: { outcome: 'analyzed', algorithm: 'Community Forensics', device: 'cuda', elapsed_seconds: 1.25, limitations: [], aigc_image: raw,
    processing: { display_preview: 'preview.png' }, model: { source_revision: 'fixed-revision' }, performance: { peak_reserved_bytes: 1024 } },
  created_at: '', review: null }
const response = (value: unknown) => ({ ok: true, json: async () => value })
function mount(element: React.ReactNode, url = '/apps/forensics/aigc?workspace=workspace') {
  const router = createMemoryRouter([{ path: '*', element }], { initialEntries: [url] })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  const view = render(<ConfigProvider><QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider></ConfigProvider>)
  return { router, ...view }
}
const view = (value = analysis, readOnly = false) => <ResultView item={value} workspaceId="workspace" refresh={() => {}}
  referenceName="" setReferenceName={() => {}} onReference={() => {}} referenceBusy={false} readOnly={readOnly} snapshotId={readOnly ? 'snapshot' : undefined} />

test('uncalibrated raw scores remain inconclusive with an original preview and same-asset SAFIRE navigation', () => {
  const { container } = mount(view())
  expect(screen.getByText('0.823457')).toBeVisible()
  expect(within(screen.getByRole('region', { name: '整图生成检测结果' })).getByText('无法判断')).toBeVisible()
  expect(screen.getByText('实验能力')).toBeVisible()
  expect(screen.getByText('未校准')).toBeVisible()
  expect(screen.getByText('截图与压缩会影响信号')).toBeVisible()
  expect(screen.getByAltText('整图生成检测原图：公开文档.png')).toHaveAttribute('src', '/api/platform/v1/workspaces/workspace/multimodal/jobs/job/files/preview.png')
  expect(screen.getByRole('link', { name: '使用 SAFIRE 定位同一原图的局部线索' })).toHaveAttribute('href', '/apps/forensics/deep?workspace=workspace&asset=asset')
  expect(screen.getByRole('link', { name: '下载已保存版本（报告与原始分数）' })).toHaveAttribute('href', '/api/platform/v1/workspaces/workspace/multimodal/jobs/job/download')
  expect(container.querySelector('[data-region], [data-suggestion]')).toBeNull()
  expect(screen.queryByRole('button', { name: '叠加' })).not.toBeInTheDocument()
  expect(container.textContent).not.toContain('82.35%')
  expect(container.textContent).not.toContain('AI 生成概率')
  expect(screen.getByRole('button', { name: '归集到成果中心' })).toBeDisabled()
})

test.each(['signal_detected', 'no_significant_signal'] as const)('a claimed %s without calibration cannot become a categorical decision', state => {
  mount(<AigcResultView value={{ ...raw, state, threshold: .5 }} preview="preview.png" sourceName="图.png" workspaceId="workspace" />)
  expect(screen.getByText('无法判断')).toBeVisible()
  expect(screen.getByText('未设置')).toBeVisible()
  expect(screen.queryByText('检出生成相关信号')).not.toBeInTheDocument()
  expect(screen.queryByText('未发现显著信号')).not.toBeInTheDocument()
})

test.each([
  { state: 'signal_detected' as const, label: '检出生成相关信号' },
  { state: 'no_significant_signal' as const, label: '未发现显著信号' },
])('calibrated $state preserves the recorded threshold and cautious label', ({ state, label }) => {
  mount(<AigcResultView value={{ ...raw, state, raw_score: state === 'signal_detected' ? .95 : .823456789, threshold: .9, calibration: { calibration_id: 'calibration-development-001', status: 'frozen' } }}
    preview="preview.png" sourceName="图.png" workspaceId="workspace" />)
  expect(screen.getByText(label)).toBeVisible()
  expect(screen.getByText('0.900000')).toBeVisible()
  expect(screen.getByText('已记录校准')).toBeVisible()
  expect(screen.getByText('calibration-development-001')).toBeVisible()
  expect(screen.queryByText('真实图片')).not.toBeInTheDocument()
})

test('pinned AIGC results keep preview and export pinned and disable editing', () => {
  mount(view(analysis, true))
  expect(screen.getByAltText('整图生成检测原图：公开文档.png')).toHaveAttribute('src', '/api/platform/v1/workspaces/workspace/multimodal/jobs/job/snapshots/snapshot/files/preview.png')
  expect(screen.getByRole('link', { name: '下载此固定版本' })).toHaveAttribute('href', '/api/platform/v1/workspaces/workspace/multimodal/jobs/job/snapshots/snapshot/download')
  expect(screen.queryByRole('button', { name: '保存复核与校正' })).not.toBeInTheDocument()
})

test('AIGC review saves source revision and evidence without fabricated region marks', async () => {
  let payload: Record<string, unknown> | undefined
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => { payload = JSON.parse(String(init?.body)); return response(analysis) }))
  mount(view())
  fireEvent.change(screen.getByText('复核人').closest('label')!.querySelector('input')!, { target: { value: '工程复核员' } })
  fireEvent.change(screen.getByPlaceholderText('填写复核依据、修改说明或待查事项'), { target: { value: '已核对来源，等待低误报校准' } })
  fireEvent.click(screen.getByRole('button', { name: '保存复核与校正' }))
  await waitFor(() => expect(payload?.expected_revision).toBe(1))
  expect(payload).toMatchObject({ actor: '工程复核员', assessment: 'inconclusive', edits: [], regions: [] })
})

test.each([true, false])('AIGC submission preserves original asset identity and uses independent GPU readiness=%s', async gpuReady => {
  let payload: Record<string, unknown> | undefined
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/multimodal/capabilities')) return response({ items: [{ kind: 'aigc_image', cpu_ready: false, gpu_ready: gpuReady, missing: [] }] })
    if (url.includes('/assets?')) return response({ items: [{ asset_id: 'asset', revision: 2, blob_hash: 'b'.repeat(64), original_name: '公开文档.png', media_type: 'image/png' }], next_offset: null })
    if (init?.method === 'POST') { payload = JSON.parse(String(init.body)); return response({ ...analysis, result: null, status: 'queued' }) }
    return response({ items: [], total: 0 })
  }))
  mount(<MultimodalWorkbench kind="aigc_image" />, '/apps/forensics/aigc?workspace=workspace&asset=asset')
  await screen.findByText('公开文档.png')
  const start = screen.getByRole('button', { name: '开始整图 AI 生成检测' })
  if (gpuReady) {
    await waitFor(() => expect(start).toBeEnabled())
    fireEvent.click(start)
    await waitFor(() => expect(payload).toBeDefined())
    expect(payload).toMatchObject({ kind: 'aigc_image', options: { device: 'cuda', action: 'analyze' }, inputs: [{ asset_id: 'asset', asset_revision: 2, blob_hash: 'b'.repeat(64) }] })
    expect(payload?.options).not.toHaveProperty('threshold')
  } else {
    expect(start).toBeDisabled()
    expect(await screen.findByText('当前设备尚未通过整图生成检测推理验证')).toBeVisible()
    expect(payload).toBeUndefined()
  }
})

test('the independent tool card keeps scope and distinguishes runtime readiness from capability qualification', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => response({ items: [{ kind: 'aigc_image', cpu_ready: false, gpu_ready: true, missing: [] }] })))
  mount(<MultimodalApplicationCard kind="aigc_image" />, '/apps?workspace=workspace&task_return=task')
  expect(await screen.findByText('GPU 推理已验证')).toBeVisible()
  expect(screen.getByText('实验能力 · 独立人工复核')).toBeVisible()
  expect(screen.getByRole('link', { name: '整图 AI 生成检测' })).toHaveAttribute('href', '/apps/forensics/aigc?workspace=workspace&task_return=task')
})

test('Agent AIGC result references open their own pinned route', () => {
  const ref = { application: 'multimodal', kind: 'result', coverage: { kind: 'aigc_image' }, object_id: 'snapshot', native_id: 'job',
    workspace_id: 'workspace', revision: 2, sha256: 'c'.repeat(64) } as AgentRef
  expect(referencePath(ref, 'task')).toBe(`/apps/forensics/aigc?workspace=workspace&task_return=task&analysis=job&snapshot=snapshot&revision=2&sha256=${'c'.repeat(64)}`)
})
