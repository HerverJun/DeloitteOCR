import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ToolDirectory } from '../src/features/catalog/ToolDirectory'

vi.mock('../src/App', () => ({
  useWorkspaceId: () => 'workspace', useWorkspaces: () => ({ data: [{ workspace_id: 'workspace', name: '当前项目' }] }),
  useApps: () => ({ data: [], isPending: false, isError: false }),
}))
vi.mock('../src/features/catalog/ApplicationCards', () => ({ ApplicationCard: () => null, RecentWorks: () => null }))
vi.mock('../src/features/data/DataWorkbench', () => ({ DataApplicationCard: () => <p>数据清洗与比对</p> }))
vi.mock('../src/features/forensics/ForensicWorkbench', () => ({ ForensicApplicationCard: () => <p>图像取证</p> }))
vi.mock('../src/features/multimodal/MultimodalWorkbench', () => ({ MultimodalApplicationCard: ({ kind }: { kind: string }) => <p>{kind}</p> }))
vi.mock('../src/features/comparisons/ComparisonApplicationCard', () => ({ ComparisonApplicationCard: () => <p>文档主题对比</p> }))
vi.mock('../src/features/agent/TaskPanel', () => ({ ReferenceLinks: () => null }))
beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
function mount() {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({}) })))
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/apps?workspace=workspace&task_return=original']}><ToolDirectory /></MemoryRouter>
  </QueryClientProvider>)
}
test.each([
  ['印章', '印章识别', '/apps/ocr/seals'],
  ['SAFIRE', 'SAFIRE 深度定位', '/apps/forensics/deep'],
  ['改字', '文档篡改排查', '/apps/forensics/document'],
])('searching %s finds its specialist tool, counts it and keeps the task context', (query, title, path) => {
  mount()
  fireEvent.change(screen.getByRole('textbox', { name: '搜索工具或用途' }), { target: { value: query } })
  expect(screen.getByRole('link', { name: new RegExp(title) })).toHaveAttribute('href', `${path}?workspace=workspace&task_return=original`)
  expect(screen.getByRole('status')).toHaveTextContent('1 个工具符合筛选')
  expect(screen.queryByText(/没有找到/)).not.toBeInTheDocument()
  expect(within(screen.getByRole('region', { name: '专项扩展' })).getAllByRole('link')).toHaveLength(1)
})
test('category filters cover specialist tools and clearing an empty search restores the complete count', () => {
  mount()
  expect(screen.getByRole('status')).toHaveTextContent('9 个工具，按需打开')
  fireEvent.click(screen.getByRole('button', { name: '文档识别' }))
  expect(screen.getByRole('status')).toHaveTextContent('3 个工具符合筛选')
  expect(screen.getByRole('link', { name: /印章识别/ })).toBeVisible()
  expect(screen.getByRole('link', { name: /文档篡改排查/ })).toBeVisible()
  expect(screen.queryByRole('link', { name: /SAFIRE/ })).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('textbox', { name: '搜索工具或用途' }), { target: { value: 'SAFIRE' } })
  expect(screen.getByText('没有找到与“SAFIRE”匹配的工具')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: /显示全部工具/ }))
  expect(screen.getByRole('status')).toHaveTextContent('9 个工具，按需打开')
  expect(screen.getByRole('link', { name: /SAFIRE 深度定位 JPEG、PNG 可疑区域辅助排查/ })).toBeVisible()
})
