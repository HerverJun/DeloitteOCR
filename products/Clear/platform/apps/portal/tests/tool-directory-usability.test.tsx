import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ToolDirectory } from '../src/features/catalog/ToolDirectory'
import { toolCatalog } from '../src/features/catalog/toolCatalog'

const registry = vi.hoisted(() => ({ data: [] as { app_id: string; display_name: string }[], isError: false }))
vi.mock('../src/App', () => ({
  useWorkspaceId: () => 'workspace', useWorkspaces: () => ({ data: [{ workspace_id: 'workspace', name: '当前项目' }] }),
  useApps: () => ({ ...registry, isPending: false, refetch: vi.fn() }),
}))
vi.mock('../src/features/catalog/ApplicationCards', () => ({ ApplicationCard: ({ app }: { app: { display_name: string } }) => <p>{app.display_name}</p>, RecentWorks: () => null }))
vi.mock('../src/features/agent/TaskPanel', () => ({ ReferenceLinks: () => null }))
beforeAll(() => {
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); registry.data = []; registry.isError = false })
function mount(query = '') {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({}) })))
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <MemoryRouter initialEntries={['/apps?workspace=workspace&task_return=original' + query]}><ToolDirectory /></MemoryRouter>
  </QueryClientProvider>)
}
test.each([
  ['印章', '印章识别', '/apps/ocr/seals'],
  ['SAFIRE', 'SAFIRE 深度定位', '/apps/forensics/deep'],
  ['改字', '文档篡改排查', '/apps/forensics/document'],
])('searching %s finds its specialist tool, counts it and keeps the task context', (query, title, path) => {
  mount()
  fireEvent.change(screen.getByRole('textbox', { name: '搜索工具或要做的事' }), { target: { value: query } })
  expect(screen.getByRole('link', { name: `打开${title}` })).toHaveAttribute('href', `${path}?workspace=workspace&task_return=original`)
  expect(screen.getByRole('status')).toHaveTextContent('1 个工具符合筛选')
  expect(screen.queryByText(/没有找到/)).not.toBeInTheDocument()
  expect(within(screen.getByRole('region', { name: '工具检索' })).getAllByRole('link')).toHaveLength(1)
})
test('category filters cover specialist tools and clearing an empty search restores the complete count', () => {
  mount()
  expect(screen.getByRole('status')).toHaveTextContent('11 个工具，选择后直接进入')
  fireEvent.click(screen.getByRole('button', { name: '文档', exact: true }))
  expect(screen.getByRole('status')).toHaveTextContent('4 个工具符合筛选')
  expect(screen.getByRole('link', { name: /印章识别/ })).toBeVisible()
  expect(screen.getByRole('link', { name: /文档篡改排查/ })).toBeVisible()
  expect(screen.queryByRole('link', { name: /SAFIRE/ })).not.toBeInTheDocument()
  fireEvent.change(screen.getByRole('textbox', { name: '搜索工具或要做的事' }), { target: { value: 'SAFIRE' } })
  expect(screen.getByText('没有找到与“SAFIRE”匹配的工具')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: /显示全部工具/ }))
  expect(screen.getByRole('status')).toHaveTextContent('11 个工具，选择后直接进入')
  expect(screen.getByRole('link', { name: '打开SAFIRE 深度定位' })).toBeVisible()
})

test('every built-in tool has one direct entry without waiting for native service state', () => {
  mount('&analysis=foreign&snapshot=old&continue=once')
  expect(within(screen.getByRole('region', { name: '工具检索' })).getAllByRole('link')).toHaveLength(11)
  for (const tool of toolCatalog) {
    expect(screen.getByRole('link', { name: `打开${tool.title}` })).toHaveAttribute('href', `${tool.path}?workspace=workspace&task_return=original`)
  }
  expect(screen.queryByText('查看工具声明')).not.toBeInTheDocument()
  expect(screen.queryByText('专项扩展')).not.toBeInTheDocument()
  expect(screen.getByRole('region', { name: '图片检查与整理' })).toContainElement(screen.getByRole('link', { name: '打开整图 AI 生成检测' }))
})

test.each([
  ['是不是AI生成的', '整图 AI 生成检测'],
  ['图片转文字', '文档识别'],
  ['录音转文字', '音频分析'],
])('a task description %s finds the intended tool', (query, title) => {
  mount()
  fireEvent.change(screen.getByRole('textbox', { name: '搜索工具或要做的事' }), { target: { value: query } })
  expect(screen.getByRole('link', { name: `打开${title}` })).toBeVisible()
  expect(screen.getByRole('status')).toHaveTextContent('1 个工具符合筛选')
})

test('unknown installed extensions remain available and a registry error does not hide built-in tools', () => {
  registry.data = [{ app_id: 'special_extension', display_name: '已安装的其他工具' }]
  registry.isError = true
  mount()
  expect(screen.getByRole('status')).toHaveTextContent('12 个工具，选择后直接进入')
  expect(screen.getByRole('region', { name: '其他已安装工具' })).toHaveTextContent('已安装的其他工具')
  expect(screen.getByRole('link', { name: '打开文档识别' })).toBeVisible()
  expect(screen.getByRole('button', { name: '重试读取' })).toBeEnabled()
})
