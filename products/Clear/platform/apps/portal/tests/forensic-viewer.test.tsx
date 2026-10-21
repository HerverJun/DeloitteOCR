import '@testing-library/jest-dom/vitest'
import { randomUUID } from 'node:crypto'
import { useState } from 'react'
import { afterEach, beforeAll, beforeEach, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { ForensicImageViewer } from '../src/features/forensics/ForensicImageViewer'
import { ForensicEvidenceView } from '../src/features/forensics/ForensicWorkbench'
import { RegionReview, type Mark } from '../src/features/multimodal/RegionReview'
import { MultimodalWorkbench, ResultView } from '../src/features/multimodal/MultimodalWorkbench'
import { displayBoxToStored, inverseMatrix, orientationMatrix, readGeometry, storedBoxToDisplay,
  type ForensicGeometry, type NormalizedBox } from '../src/features/forensics/geometry'
import type { Inspection } from '../../../packages/contracts/types.generated'

vi.mock('../src/App', () => ({ useWorkspaceId: () => 'wForensic', Workspaces: () => null }))
beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
})
beforeEach(() => {
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals(); localStorage.clear() })

function geometry(orientation = 1): ForensicGeometry {
  const forward = orientationMatrix(orientation, [400, 200])
  return { schema_version: '1.0', coordinate_version: 'exif-pixel-edges-v1', source_coordinate_system: 'stored_pixels',
    display_coordinate_system: 'exif_transposed_pixels', coordinate_convention: 'pixel_edges', stored_size: [400, 200],
    display_size: orientation >= 5 ? [200, 400] : [400, 200], exif_orientation: orientation,
    orientation_status: 'valid', stored_to_display: forward, display_to_stored: inverseMatrix(forward),
    persisted_region_coordinate_system: 'stored_pixels' }
}
const expected: NormalizedBox[] = [
  [.1, .2, .4, .7], [.6, .2, .9, .7], [.6, .3, .9, .8], [.1, .3, .4, .8],
  [.2, .1, .7, .4], [.3, .1, .8, .4], [.3, .6, .8, .9], [.2, .6, .7, .9],
]
test.each(expected.map((box, index) => [index + 1, box] as const))('EXIF %s maps rectangle corners to the expected display box and back', (orientation, box) => {
  const info = geometry(orientation)
  expect(readGeometry(info)).not.toBeNull()
  const mapped = storedBoxToDisplay([.1, .2, .4, .7], info)
  mapped.forEach((value, index) => expect(value).toBeCloseTo(box[index], 10))
  displayBoxToStored(mapped, info).forEach((value, index) => expect(value).toBeCloseTo([.1, .2, .4, .7][index], 10))
})

test('unknown versions, invalid EXIF, swapped sizes and inconsistent inverse matrices are rejected', () => {
  expect(readGeometry(undefined)).toBeNull()
  expect(readGeometry({ ...geometry(), coordinate_version: 'guess-v0' })).toBeNull()
  expect(readGeometry({ ...geometry(), exif_orientation: 9 })).toBeNull()
  expect(readGeometry({ ...geometry(), orientation_status: 'invalid' })).toBeNull()
  expect(readGeometry({ ...geometry(6), display_size: [400, 200] })).toBeNull()
  expect(readGeometry({ ...geometry(6), display_to_stored: orientationMatrix(1, [400, 200]) })).toBeNull()
})

function loadProbe(container: HTMLElement, src: string, width: number, height: number) {
  const image = Array.from(container.querySelectorAll<HTMLImageElement>('.forensic-image-probe')).find(value => value.getAttribute('src') === src)!
  Object.defineProperties(image, { naturalWidth: { configurable: true, value: width }, naturalHeight: { configurable: true, value: height } })
  fireEvent.load(image)
}

test('overlay is only enabled after dimensions match, defaults to 50%, and zoom preserves one SVG coordinate system', () => {
  const { container } = render(<ForensicImageViewer image="/original.png" heatmap="/heat.png" width={400} height={200}
    registered marks={[{ box: [.1, .2, .4, .7], note: '待查' }]} />)
  expect(screen.getByRole('button', { name: '叠加' })).toBeDisabled()
  loadProbe(container, '/original.png', 400, 200); loadProbe(container, '/heat.png', 400, 200)
  fireEvent.click(screen.getByRole('button', { name: '叠加' }))
  const heat = container.querySelector('[data-layer="heatmap"]')!
  expect(heat).toHaveAttribute('opacity', '0.5')
  expect(heat.parentElement).toBe(container.querySelector('[data-layer="original"]')!.parentElement)
  fireEvent.change(screen.getByRole('slider', { name: '热图透明度' }), { target: { value: '75' } })
  expect(heat).toHaveAttribute('opacity', '0.75')
  fireEvent.click(screen.getByRole('button', { name: '1:1' }))
  const canvas = container.querySelector('svg[viewBox]')!
  expect(canvas).toHaveAttribute('width', '400')
  expect(canvas).toHaveStyle({ width: '400px', height: '200px' })
  fireEvent.click(screen.getByRole('button', { name: '放大图像' }))
  expect(canvas).toHaveAttribute('width', '500')
  expect(canvas).toHaveAttribute('viewBox', '0 0 400 200')
  expect(container.querySelector('[data-region="0"]')).toHaveAttribute('x', '40')
  fireEvent.click(screen.getByRole('button', { name: '热图' }))
  expect(container.querySelector('[data-layer="original"]')).toBeNull()
  expect(container.querySelector('[data-region="0"]')).not.toBeNull()
})

test('a mismatched map remains independently viewable and cannot be stretched over the original', () => {
  const { container } = render(<ForensicImageViewer image="/original.png" heatmap="/wrong.png" width={400} height={200} registered />)
  loadProbe(container, '/original.png', 400, 200); loadProbe(container, '/wrong.png', 200, 100)
  expect(screen.getByText(/实际尺寸或坐标信息不一致/)).toBeVisible()
  expect(screen.getByRole('button', { name: '叠加' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '热图' }))
  expect(screen.getByRole('img', { name: /独立查看/ })).toHaveAttribute('src', '/wrong.png')
  expect(container.querySelector('svg[viewBox]')).toBeNull()
})

test('changing heatmaps retains the loaded original while checking the new map', () => {
  const view = render(<ForensicImageViewer image="/original.png" heatmap="/first.png" width={400} height={200} registered />)
  loadProbe(view.container, '/original.png', 400, 200); loadProbe(view.container, '/first.png', 400, 200)
  expect(screen.getByRole('button', { name: '叠加' })).toBeEnabled()
  view.rerender(<ForensicImageViewer image="/original.png" heatmap="/second.png" width={400} height={200} registered />)
  expect(screen.getByRole('button', { name: '叠加' })).toBeDisabled()
  loadProbe(view.container, '/second.png', 400, 200)
  expect(screen.getByRole('button', { name: '叠加' })).toBeEnabled()
})

test('changing the original never reuses a previous source dimension check', () => {
  const view = render(<ForensicImageViewer image="/original.png" heatmap="/heat.png" width={400} height={200} registered />)
  loadProbe(view.container, '/original.png', 400, 200); loadProbe(view.container, '/heat.png', 400, 200)
  fireEvent.click(screen.getByRole('button', { name: '叠加' }))
  view.rerender(<ForensicImageViewer image="/replacement.png" heatmap="/heat.png" width={400} height={200} registered />)
  expect(screen.getByRole('button', { name: '叠加' })).toBeDisabled()
  expect(view.container.querySelector('[data-layer="heatmap"]')).toBeNull()
  loadProbe(view.container, '/replacement.png', 400, 200)
  expect(screen.getByRole('button', { name: '叠加' })).toBeEnabled()
})
test('CAT display percentages and notes retain the stored normalized onChange contract for rotated images', () => {
  const onChange = vi.fn()
  const original: Mark = { box: [.1, .2, .4, .7], note: '原说明' }
  render(<RegionReview image="/display.png" width={400} height={200} marks={[original]} onChange={onChange}
    forensic={{ heatmap: '/heat.png', geometry: geometry(6), registered: true }} />)
  expect(screen.getByRole('spinbutton', { name: '区域1左边界百分比' })).toHaveValue('30.00')
  expect(screen.getByRole('spinbutton', { name: '区域1上边界百分比' })).toHaveValue('10.00')
  fireEvent.change(screen.getByLabelText('区域1说明'), { target: { value: '保留原坐标' } })
  expect(onChange).toHaveBeenLastCalledWith([{ ...original, note: '保留原坐标' }])
  fireEvent.click(screen.getByRole('button', { name: '添加可疑区域' }))
  const added = onChange.mock.lastCall![0][1] as Mark
  added.box.forEach((value, index) => expect(value).toBeCloseTo([.1, .6, .4, .9][index], 10))
  expect(added.note).toBe('待复核区域')
})

test('drawing on a zoomed display maps the drag back into stored coordinates', () => {
  vi.stubGlobal('PointerEvent', MouseEvent)
  let latest: Mark[] = []
  function Interactive() {
    const [marks, setMarks] = useState<Mark[]>([])
    return <RegionReview image="/display.png" width={400} height={200} marks={marks}
      onChange={next => { latest = next; setMarks(next) }} forensic={{ heatmap: '/heat.png', geometry: geometry(7), registered: true }} />
  }
  const { container } = render(<Interactive />)
  loadProbe(container, '/display.png', 200, 400); loadProbe(container, '/heat.png', 200, 400)
  fireEvent.click(screen.getByRole('button', { name: '1:1' }))
  fireEvent.click(screen.getByRole('button', { name: '放大图像' }))
  const canvas = container.querySelector('svg[viewBox]')!
  vi.spyOn(canvas, 'getBoundingClientRect').mockReturnValue({ x: 10, y: 20, left: 10, top: 20, width: 250, height: 500, right: 260, bottom: 520, toJSON() {} })
  fireEvent.pointerDown(canvas, { clientX: 60, clientY: 70, button: 0 })
  fireEvent.pointerMove(canvas, { clientX: 185, clientY: 220, button: 0 })
  fireEvent.pointerUp(canvas, { clientX: 185, clientY: 220, button: 0 })
  expect(latest).toHaveLength(1)
  latest[0].box.forEach((value, index) => expect(value).toBeCloseTo([.6, .3, .9, .8][index], 10))
})

function inspection(info?: ForensicGeometry): Inspection {
  return { schema_version: '1.0', inspection_id: 'iForensic', workspace_id: 'wForensic', request_id: 'rForensic',
    revision: 2, asset_id: 'aImage', asset_revision: 1, blob_hash: 'a'.repeat(64), original_name: '公开样本.jpg',
    tools: ['noise'], status: 'succeeded', error: null, report_hash: 'b'.repeat(64), review: null, review_history: [],
    files: [], created_at: '', updated_at: '', report: { input: { width: 400, height: 200, geometry: info, preview_path: 'preview-exif-normalized.png' },
      evidence: [{ tool: 'noise', status: 'completed', version: '0.1.1', elapsed_ms: 1, map_path: 'noise.png',
        reason: null, limitations: [], metrics: info ? { map_coordinate_system: 'exif_transposed_pixels', coordinate_version: info.coordinate_version } : {} }] } }
}

test('classic new reports share the viewer read-only, and legacy reports explicitly disable registration', () => {
  const view = render(<MemoryRouter><ForensicEvidenceView inspection={inspection(geometry())} /></MemoryRouter>)
  loadProbe(view.container, '/api/platform/v1/workspaces/wForensic/forensics/iForensic/files/preview-exif-normalized.png', 400, 200)
  loadProbe(view.container, '/api/platform/v1/workspaces/wForensic/forensics/iForensic/files/noise.png', 400, 200)
  expect(screen.getByRole('button', { name: '叠加' })).toBeEnabled()
  expect(screen.queryByRole('button', { name: '添加可疑区域' })).toBeNull()
  view.rerender(<MemoryRouter><ForensicEvidenceView inspection={inspection()} /></MemoryRouter>)
  expect(screen.getByText(/旧版报告缺少可核验的坐标元数据/)).toBeVisible()
  expect(screen.getByRole('button', { name: '叠加' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '热图' }))
  expect(screen.getByRole('img', { name: /独立查看/ })).toBeInTheDocument()
})

test('CAT transformed marks save through the existing review payload and legacy results preserve stored previews', async () => {
  vi.stubGlobal('crypto', { randomUUID })
  const info = geometry(6)
  const item = { job_id: 'mCat', kind: 'catnet' as const, status: 'succeeded', stage: 'completed', revision: 3,
    error: null, result_hash: 'c'.repeat(64), inputs: [{ original_name: '公开样本.jpg', asset_id: 'aImage', asset_revision: 2 }],
    result: { outcome: 'analyzed', algorithm: 'CAT-Net v2', device: 'cpu', elapsed_seconds: 1, limitations: [], width: 400, height: 200,
      processing: { geometry: info, display_preview: 'preview-display.png', display_heatmap: 'heatmap-display.png' },
      output_files: { 'preview-display.png': { bytes: 10, sha256: 'd'.repeat(64) }, 'heatmap-display.png': { bytes: 10, sha256: 'e'.repeat(64) } } },
    created_at: '', review: null }
  let payload: { expected_revision: number; regions: Mark[] } | undefined
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    payload = JSON.parse(init!.body as string)
    return { ok: true, json: async () => ({ ...item, revision: 4, review: payload }) }
  }))
  const mount = (value: typeof item) => render(<ConfigProvider><QueryClientProvider client={new QueryClient()}>
    <MemoryRouter><ResultView item={value} workspaceId="wForensic" refresh={() => {}} referenceName="" setReferenceName={() => {}} onReference={() => {}} referenceBusy={false} /></MemoryRouter>
  </QueryClientProvider></ConfigProvider>)
  const view = mount(item)
  expect(screen.getByText('CAT-Net v2')).toBeVisible()
  expect(screen.queryByText('未校准定位线索')).toBeNull()
  expect(view.container.querySelector('image[data-layer="original"]')).toHaveAttribute('href', '/api/platform/v1/workspaces/wForensic/multimodal/jobs/mCat/files/preview-display.png')
  fireEvent.click(screen.getByRole('button', { name: '添加可疑区域' }))
  fireEvent.change(screen.getByLabelText('区域1说明'), { target: { value: '已对照原件核查' } })
  fireEvent.change(screen.getByText('复核人').closest('label')!.querySelector('input')!, { target: { value: '人工复核员' } })
  fireEvent.change(screen.getByPlaceholderText('填写复核依据、修改说明或待查事项'), { target: { value: '当前仍无法判断' } })
  fireEvent.click(screen.getByRole('button', { name: '保存复核与校正' }))
  await waitFor(() => expect(payload?.regions).toHaveLength(1))
  expect(payload!.expected_revision).toBe(3)
  payload!.regions[0].box.forEach((value, index) => expect(value).toBeCloseTo([.1, .6, .4, .9][index], 10))
  expect(payload!.regions[0].note).toBe('已对照原件核查')
  cleanup()
  const legacy = mount({ ...item, job_id: 'mLegacy', result: { ...item.result, processing: undefined, output_files: undefined } } as unknown as typeof item)
  expect(screen.getByText(/保留历史原图与原保存坐标/)).toBeVisible()
  expect(legacy.container.querySelector('image[data-layer="original"]')).toHaveAttribute('href', '/api/platform/v1/workspaces/wForensic/multimodal/jobs/mLegacy/files/preview.png')
  expect(screen.getByRole('button', { name: '叠加' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '添加可疑区域' }))
  expect(screen.getByRole('spinbutton', { name: '区域1左边界百分比' })).toHaveValue('10.00')
}, 30000)

test('already oriented SAFIRE geometry is accepted only as an identity transform', () => {
  const oriented = { ...geometry(), source_coordinate_system: 'exif_transposed_pixels', persisted_region_coordinate_system: 'exif_transposed_pixels' }
  expect(readGeometry(oriented)).not.toBeNull()
  expect(readGeometry({ ...oriented, exif_orientation: 6 })).toBeNull()
  expect(readGeometry({ ...oriented, persisted_region_coordinate_system: 'stored_pixels' })).toBeNull()
})

test.each(['catnet', 'document_forensics'] as const)('SAFIRE %s suggestions share oriented pixels and are explicitly adopted into saved review', async kind => {
  vi.stubGlobal('crypto', { randomUUID })
  const info = { ...geometry(), source_coordinate_system: 'exif_transposed_pixels' as const, persisted_region_coordinate_system: 'exif_transposed_pixels' }
  const item = { job_id: 'mSafire', kind, status: 'succeeded', stage: 'completed', revision: 1, error: null,
    result_hash: 'a'.repeat(64), inputs: [{ original_name: '金额.png', asset_id: 'aSafire', asset_revision: 1 }], created_at: '', review: null,
    result: { outcome: 'analyzed', algorithm: 'SAFIRE (official)', engine: 'SAFIRE', detector: { version: 'clear-safire-product-v1' },
      device: 'cuda', elapsed_seconds: 2, limitations: ['定位尚未独立校准'], width: 400, height: 200,
      assessment: { state: 'review_recommended', label: '建议复核', reason: '存在建议区域', calibration: false }, deep_analysis: { status: 'completed' },
      candidate_regions: [{ box: [.1, .2, .4, .7] as Mark['box'], pixel_box: [40, 40, 160, 140], score: .3 }],
      processing: { geometry: info, display_preview: 'preview-display.png', display_heatmap: 'heatmap-display.png' },
      output_files: { 'preview-display.png': { bytes: 1, sha256: 'a'.repeat(64) }, 'heatmap-display.png': { bytes: 1, sha256: 'b'.repeat(64) } } } }
  let payload: { regions: Mark[] } | undefined
  vi.stubGlobal('fetch', vi.fn(async (_url: string, init?: RequestInit) => {
    payload = JSON.parse(String(init?.body)); return { ok: true, json: async () => ({ ...item, review: payload }) }
  }))
  const view = render(<ConfigProvider><QueryClientProvider client={new QueryClient()}><MemoryRouter>
    <ResultView item={item} workspaceId="wForensic" refresh={() => {}} referenceName="" setReferenceName={() => {}} onReference={() => {}} referenceBusy={false} />
  </MemoryRouter></QueryClientProvider></ConfigProvider>)
  expect(screen.getByText('建议复核')).toBeVisible()
  expect(screen.getByText('未校准定位线索')).toBeVisible()
  expect(screen.getByText('clear-safire-product-v1')).toBeVisible()
  expect(view.container.querySelector('[data-suggestion="0"]')).toHaveAttribute('x', '40')
  expect(view.container.querySelector('[data-region="0"]')).toBeNull()
  loadProbe(view.container, '/api/platform/v1/workspaces/wForensic/multimodal/jobs/mSafire/files/preview-display.png', 400, 200)
  loadProbe(view.container, '/api/platform/v1/workspaces/wForensic/multimodal/jobs/mSafire/files/heatmap-display.png', 400, 200)
  expect(screen.getByRole('button', { name: '叠加' })).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: '隐藏建议框' }))
  expect(view.container.querySelector('[data-suggestion="0"]')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '采纳建议区域 1' }))
  expect(screen.getByRole('button', { name: '采纳建议区域 1' })).toBeDisabled()
  fireEvent.change(screen.getByLabelText('区域1说明'), { target: { value: '金额改字，需核原件' } })
  fireEvent.change(screen.getByText('复核人').closest('label')!.querySelector('input')!, { target: { value: '复核员' } })
  fireEvent.change(screen.getByPlaceholderText('填写复核依据、修改说明或待查事项'), { target: { value: '已对照原图' } })
  fireEvent.click(screen.getByRole('button', { name: '保存复核与校正' }))
  await waitFor(() => expect(payload?.regions).toHaveLength(1))
  expect(payload!.regions[0]).toEqual({ box: [.1, .2, .4, .7], note: '金额改字，需核原件' })
  expect(screen.queryByText(/真实性.*%/)).toBeNull()
  cleanup()
  const reopened = render(<ConfigProvider><QueryClientProvider client={new QueryClient()}><MemoryRouter>
    <ResultView item={{ ...item, review: { actor: '复核员', note: '已对照原图', assessment: 'inconclusive', edits: [], regions: payload!.regions } }}
      workspaceId="wForensic" refresh={() => {}} referenceName="" setReferenceName={() => {}} onReference={() => {}} referenceBusy={false} readOnly snapshotId="saved" />
  </MemoryRouter></QueryClientProvider></ConfigProvider>)
  expect(reopened.container.querySelector('[data-region="0"]')).toHaveAttribute('x', '40')
  expect(screen.getByLabelText('区域1说明')).toHaveValue('金额改字，需核原件')
  expect(screen.getByLabelText('区域1说明')).toBeDisabled()
  expect(screen.getByRole('button', { name: '采纳建议区域 1' })).toBeDisabled()
  expect(screen.getByRole('link', { name: '下载此固定版本' })).toHaveAttribute('href', '/api/platform/v1/workspaces/wForensic/multimodal/jobs/mSafire/snapshots/saved/download')
}, 30000)

test.each([true, false])('deep PNG creation uses GPU qualification rather than CPU readiness (GPU ready=%s)', async gpuReady => {
  vi.stubGlobal('crypto', { randomUUID })
  let submitted: { kind: string; options: { device: string }; inputs: { asset_id: string }[] } | undefined
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    let result: unknown = { items: [], total: 0, next_offset: null }
    if (url.includes('/multimodal/capabilities')) result = { items: [{ kind: 'catnet', cpu_ready: false, gpu_ready: gpuReady, missing: [] }] }
    else if (url.includes('/assets?')) result = { items: [{ asset_id: 'aPng', revision: 1, original_name: '正常.png', media_type: 'image/png', blob_hash: 'a'.repeat(64) }], next_offset: null }
    else if (init?.method === 'POST') {
      submitted = JSON.parse(String(init.body))
      result = { job_id: 'newPng', kind: 'catnet', status: 'queued', stage: 'queued', inputs: [], revision: 1, result: null }
    }
    return { ok: true, json: async () => result }
  }))
  render(<ConfigProvider><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/apps/forensics/deep?workspace=wForensic&asset=aPng']}><MultimodalWorkbench kind="catnet" /></MemoryRouter>
  </QueryClientProvider></ConfigProvider>)
  await screen.findByText('正常.png')
  const start = screen.getByRole('button', { name: '开始SAFIRE 深度定位' })
  if (gpuReady) {
    await waitFor(() => expect(start).toBeEnabled())
    fireEvent.click(start)
    await waitFor(() => expect(submitted?.options.device).toBe('cuda'))
    expect(submitted?.kind).toBe('catnet')
    expect(submitted?.inputs[0].asset_id).toBe('aPng')
  } else {
    expect(start).toBeDisabled()
    expect(await screen.findByText('当前设备尚未通过 SAFIRE 推理验证')).toBeVisible()
    expect(submitted).toBeUndefined()
  }
}, 30000)
