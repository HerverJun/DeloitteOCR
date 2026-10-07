import '@testing-library/jest-dom/vitest'
import { afterEach, beforeAll, expect, test, vi } from 'vitest'
import { cleanup, configure, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, MemoryRouter, RouterProvider } from 'react-router-dom'
import { SkillsPanel } from '../src/features/agent/SkillsPanel'
import { ControllerSettings } from '../src/features/agent/ControllerSettings'
import { AgentWorkbench, EventContent } from '../src/features/agent/AgentWorkbench'
import { PlatformSettings } from '../src/features/agent/PlatformSettings'
import type { AgentSkill, ContextBudget } from '../src/features/agent/api'

vi.mock('../src/App', () => ({ useWorkspaces: () => ({ data: [] }), useWorkspaceId: () => null }))
beforeAll(() => {
  configure({ asyncUtilTimeout: 10000 })
  window.matchMedia = vi.fn().mockImplementation(() => ({ matches: false, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn() }))
  globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
  const computed = window.getComputedStyle.bind(window)
  vi.spyOn(window, 'getComputedStyle').mockImplementation(element => computed(element))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); localStorage.clear(); sessionStorage.clear() })
function mount(element: React.ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}>{element}</QueryClientProvider>)
}
const budget: ContextBudget = { estimation_method: 'utf8_bytes_div2_estimate', payload_bytes: 4000, input_tokens_estimate: 2000,
  output_reserve: 2048, context_cap: 16384, component_tokens: { system: 300, environment: 200, skills: 150, tools: 750, user: 20, facts: 100, tool_results: 300, assistant: 80, overhead: 100 }, selected_tools: ['tools_list', 'skills_activate'], reclaimed_tools: ['ocr_read'], compressed: true }
const skill: AgentSkill = { name: 'payment-reconciliation', description: '付款台账核对流程', source: 'platform:payment-reconciliation', version: 'b'.repeat(64),
  compatibility: 'Clear registered tools', compatible: true, reason: '', allowed_tools: ['data_reconcile'], resources: [{ path: 'references/rules.md', kind: 'reference', sha256: 'c'.repeat(64), bytes: 8000 }],
  active: false, active_version: null, source_changed: false }
const page = (items: AgentSkill[], active: AgentSkill[] = [], extra = {}) => ({ items, active, total: items.length, next_offset: null, rejected: [], ...extra })
const event = (kind: string, payload: Record<string, unknown>) => <EventContent event={{ seq: 1, kind, run_id: 'run', created: '', payload }} reply={() => {}} answered={false} answerable={false} />

test('request composition is explicitly estimated and provider usage remains separate', () => {
  mount(<>{event('model_request', { model: 'configured-model', context_metrics: budget })}{event('usage', { input_tokens: 1872, output_tokens: 95 })}</>)
  expect(screen.getByLabelText('模型请求上下文预算')).toHaveTextContent('输入估计 2,000 tokens')
  expect(screen.getByLabelText('服务返回实际用量')).toHaveTextContent('输入 1,872 tokens · 输出 95 tokens')
  fireEvent.click(screen.getByText('查看预算组成与工具加载'))
  for (const label of ['系统说明', '执行环境', '技能指导', '工具契约', '用户消息', '任务与来源事实', '工具结果', '输出预留']) expect(screen.getByText(label, { selector: 'dt' })).toBeInTheDocument()
  expect(screen.getByText(/UTF-8 字节保守估计/)).toBeInTheDocument()
  expect(screen.getByText('skills_activate')).toBeInTheDocument()
  expect(screen.getByText(/收回 1 个无关契约/)).toBeInTheDocument()
})

test('context error identifies the overflowing component rather than blaming a short message', () => {
  mount(event('error', { message: '本地预算不足', context_budget: { ...budget, overflow_component: 'skills', overflow_tokens: 1300 } }))
  expect(screen.getByText('本地预算不足')).toBeInTheDocument()
  expect(screen.getByText('超限组成：技能指导，约 150 tokens')).toBeInTheDocument()
  expect(screen.getByText(/普通短消息也可能因工具或技能内容过多/)).toBeInTheDocument()
})

test('environment evidence displays configured identity and never dumps arbitrary credentials', () => {
  mount(event('context_injection', { version: 'environment-v2', environment: { controller: { configured_model: 'configured-id', protocol: 'openai_chat_completions', connection_revision: 5, api_key: 'secret-controller' },
    execution_environment: 'Windows native services', unrelated_path: 'secret-path' } }))
  expect(screen.getByText('配置模型：configured-id')).toBeInTheDocument()
  expect(screen.getByText('连接版本：5')).toBeInTheDocument()
  expect(screen.queryByText(/secret-controller|secret-path/)).not.toBeInTheDocument()
})

test('directory paging and search retain workspace/session while inactive skills cannot be read', async () => {
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string) => { urls.push(input); return { ok: true, json: async () => input.includes('offset=20') ? page([{ ...skill, name: 'later-skill' }]) : page([skill], [], { total: 21, next_offset: 20 }) } }))
  mount(<SkillsPanel open close={() => {}} workspace="project-a" sessionId="session-a" />)
  const item = await screen.findByLabelText('可用技能 payment-reconciliation')
  expect(within(item).queryByRole('button', { name: '查看正文与资源' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '下一页' }))
  expect(await screen.findByLabelText('可用技能 later-skill')).toBeInTheDocument()
  fireEvent.change(screen.getByRole('searchbox', { name: '搜索技能' }), { target: { value: '付款' } })
  await waitFor(() => expect(urls.some(url => url.includes('keyword=%E4%BB%98%E6%AC%BE') && url.includes('offset=0'))).toBe(true))
  for (const url of urls) expect(url).toContain('workspace_id=project-a&session_id=session-a')
  expect(screen.getByText(/Agent 可按目标自行发现和激活技能/)).toBeInTheDocument()
})

test('activation retry reuses the command identity and reads fixed snapshots only after success', async () => {
  const commands: Record<string, unknown>[] = [], urls: string[] = []
  let active = false, failed = false
  const fixed = { ...skill, active: true, active_version: skill.version }
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    urls.push(input)
    if (input.endsWith('/skills/activate')) {
      commands.push(JSON.parse(String(init?.body)))
      if (!failed) { failed = true; active = true; throw new Error('回包未知，请重试') }
      active = true; return { ok: true, json: async () => ({ active: [fixed], skill: fixed, changed: true }) }
    }
    if (input.includes('/resources?')) return { ok: true, json: async () => ({ name: skill.name, version: skill.version, path: 'SKILL.md', sha256: 'd'.repeat(64), content: '固定技能指导正文', encoding: 'utf-8', total_chars: 8, next_offset: null, kind: 'instructions', source_changed: false }) }
    return { ok: true, json: async () => page([active ? fixed : skill], active ? [fixed] : []) }
  }))
  mount(<SkillsPanel open close={() => {}} workspace="project" sessionId="session" />)
  fireEvent.click(await screen.findByRole('button', { name: '激活', exact: true }))
  expect(await screen.findByText('回包未知，请重试')).toBeInTheDocument()
  expect(urls.some(url => url.includes('/resources?'))).toBe(false)
  fireEvent.click(screen.getByRole('button', { name: '重试原激活操作 · payment-reconciliation' }))
  const fixedItem = await screen.findByLabelText('已激活技能 payment-reconciliation')
  expect(commands).toHaveLength(2); expect(commands[0]).toEqual(commands[1])
  expect(commands[0]).toMatchObject({ name: skill.name, version: skill.version })
  fireEvent.click(within(fixedItem).getByRole('button', { name: '查看正文与资源' }))
  expect(await screen.findByText('固定技能指导正文')).toBeInTheDocument()
  expect(urls.at(-1)).toContain('/agent/sessions/session/skills/payment-reconciliation/resources?path=SKILL.md')
  expect(sessionStorage.length).toBe(0)
})

test('changed source keeps fixed version visible, resources paginate and explicit deactivation closes reading', async () => {
  const fixed = { ...skill, active: true, active_version: 'a'.repeat(64), version: 'a'.repeat(64), source_changed: true }
  const current = { ...skill, active: true, active_version: fixed.active_version, source_changed: true }
  let stopped = false; const commands: Record<string, unknown>[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    if (input.endsWith('/skills/deactivate')) { commands.push(JSON.parse(String(init?.body))); stopped = true; return { ok: true, json: async () => ({ active: [], skill: fixed, changed: true }) } }
    if (input.includes('/resources?')) return { ok: true, json: async () => ({ name: skill.name, version: fixed.version, path: 'SKILL.md', sha256: 'd'.repeat(64), content: input.includes('offset=4000') ? '下一段固定指导' : '首段固定指导', encoding: 'utf-8', total_chars: 4500, next_offset: input.includes('offset=4000') ? null : 4000, kind: 'instructions', source_changed: true }) }
    return { ok: true, json: async () => page([stopped ? skill : current], stopped ? [] : [fixed]) }
  }))
  mount(<SkillsPanel open close={() => {}} workspace="project" sessionId="session" />)
  const active = await screen.findByLabelText('已激活技能 payment-reconciliation')
  expect(active).toHaveTextContent('固定版本 aaaaaaaaaaaa')
  expect(within(active).getByText(/源文件已有变化/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '更新并激活' })).toBeEnabled()
  fireEvent.click(within(active).getByRole('button', { name: '查看正文与资源' }))
  expect(await screen.findByText('首段固定指导')).toBeInTheDocument()
  fireEvent.click(within(screen.getByLabelText('技能正文与资源')).getByRole('button', { name: '下一段' }))
  expect(await screen.findByText('下一段固定指导')).toBeInTheDocument()
  fireEvent.click(within(active).getByRole('button', { name: '停用' }))
  await waitFor(() => expect(screen.queryByLabelText('技能正文与资源')).not.toBeInTheDocument())
  expect(commands[0]).toMatchObject({ name: skill.name }); expect(commands[0]).not.toHaveProperty('version')
})

test('running tasks and incompatible registrations cannot be manually activated', async () => {
  const incompatible = { ...skill, compatible: false, reason: '缺少 data_reconcile 工具' }
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => page([incompatible]) })))
  const view = mount(<SkillsPanel open close={() => {}} workspace="project" sessionId="session" running />)
  expect(await screen.findByText('缺少 data_reconcile 工具')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '激活', exact: true })).toBeDisabled()
  expect(screen.getByText('任务正在执行；停止或切换人工处理后可调整技能。')).toBeInTheDocument()
  view.unmount()
})

test('registered reference resources load on demand and an explicit upgrade names the new version', async () => {
  const fixed = { ...skill, active: true, active_version: 'a'.repeat(64), version: 'a'.repeat(64), source_changed: true }
  const current = { ...skill, active: true, active_version: fixed.version, source_changed: true }
  const urls: string[] = []; let upgrade: Record<string, unknown> | undefined
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    urls.push(input)
    if (input.endsWith('/activate')) { upgrade = JSON.parse(String(init?.body)); return { ok: true, json: async () => ({ active: [current], skill: current, changed: true }) } }
    if (input.includes('/resources?')) return { ok: true, json: async () => ({ name: skill.name, version: fixed.version, path: input.includes('references') ? 'references/rules.md' : 'SKILL.md', sha256: 'c'.repeat(64), content: input.includes('references') ? '固定核对口径参考' : '固定正文', encoding: 'utf-8', total_chars: 20, next_offset: null, kind: 'reference', source_changed: true }) }
    return { ok: true, json: async () => page([current], [fixed]) }
  }))
  mount(<SkillsPanel open close={() => {}} workspace="project" sessionId="session" />)
  const active = await screen.findByLabelText('已激活技能 payment-reconciliation')
  expect(urls.some(url => url.includes('/resources?'))).toBe(false)
  fireEvent.click(within(active).getByRole('button', { name: '查看正文与资源' }))
  expect(await screen.findByText('固定正文')).toBeInTheDocument()
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '技能资源' }))
  fireEvent.click(await screen.findByText('参考资料 · references/rules.md', { selector: '.ant-select-item-option-content' }))
  expect(await screen.findByText('固定核对口径参考')).toBeInTheDocument()
  expect(urls.some(url => url.includes('path=references%2Frules.md'))).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: '更新并激活' }))
  await waitFor(() => expect(upgrade).toMatchObject({ name: skill.name, version: skill.version }))
})

test('unsupported script resources explain the host boundary without content access or paging', async () => {
  const fixed = { ...skill, active: true, active_version: skill.version,
    resources: [{ path: 'scripts/example.py', kind: 'script', sha256: 'e'.repeat(64), bytes: 80 }] }
  const urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    urls.push(input); expect(init?.method).toBeUndefined()
    if (input.includes('/resources?')) return { ok: true, json: async () => input.includes('scripts%2F') ? {
      name: skill.name, version: skill.version, path: 'scripts/example.py', kind: 'script', unsupported: true,
      reason: '当前宿主不执行技能脚本；需将功能注册为明确参数与输出的业务工具',
    } : { name: skill.name, version: skill.version, path: 'SKILL.md', sha256: 'd'.repeat(64), content: '技能正文', encoding: 'utf-8', total_chars: 4, next_offset: null, kind: 'instructions', source_changed: false } }
    return { ok: true, json: async () => page([fixed], [fixed]) }
  }))
  mount(<SkillsPanel open close={() => {}} workspace="project" sessionId="session" />)
  const active = await screen.findByLabelText('已激活技能 payment-reconciliation')
  fireEvent.click(within(active).getByRole('button', { name: '查看正文与资源' }))
  expect(await screen.findByText('技能正文', { selector: 'pre' })).toBeInTheDocument()
  fireEvent.mouseDown(screen.getByRole('combobox', { name: '技能资源' }))
  fireEvent.click(await screen.findByText('脚本资源 · scripts/example.py', { selector: '.ant-select-item-option-content' }))
  expect(await screen.findByText('此宿主不支持执行技能脚本')).toBeInTheDocument()
  const reader = screen.getByLabelText('技能正文与资源')
  expect(within(reader).getByText(/需将功能注册为明确参数与输出的业务工具/)).toBeInTheDocument()
  expect(within(reader).queryByRole('button', { name: '上一段' })).not.toBeInTheDocument()
  expect(within(reader).queryByRole('button', { name: '下一段' })).not.toBeInTheDocument()
  expect(urls.at(-1)).toContain('path=scripts%2Fexample.py')
})

test('controller save explains local capacity and displays successful production preflight separately', async () => {
  let saved = false
  vi.stubGlobal('fetch', vi.fn(async (_input: string, init?: RequestInit) => {
    if (init?.method === 'PUT') saved = true
    return { ok: true, json: async () => ({ configured: true, available: true, protocol: 'openai_chat_completions', base_url: 'http://127.0.0.1:9988/v1', model: 'test-controller', context_cap: 16384, max_output_tokens: 2048, total_seconds: 180, streaming: true,
      ...(saved ? { preflight: { context_metrics: budget } } : {}) }) }
  }))
  const close = vi.fn()
  mount(<ControllerSettings open close={close} />)
  expect(await screen.findByDisplayValue('test-controller')).toBeInTheDocument()
  expect(screen.getByText('本地上下文预算')).toBeInTheDocument()
  expect(screen.getByText(/不会自动证明或改变模型的真实容量/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '测试并保存' }))
  expect(await screen.findByText('主控连接已测试并保存')).toBeInTheDocument()
  expect(screen.getByLabelText('模型请求上下文预算')).toHaveTextContent('输入估计 2,000 tokens')
  expect(close).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '完成' })); expect(close).toHaveBeenCalledOnce()
})

test('browsing skills without a project makes only a catalog read and keeps the drafted goal', async () => {
  const writes: { path: string; body: Record<string, unknown> }[] = [], urls: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    urls.push(input)
    if (init?.method === 'POST') { writes.push({ path: input, body: JSON.parse(String(init.body)) }); return { ok: true, json: async () => ({ workspace_id: 'created-project', name: '智能审计项目' }) } }
    return { ok: true, json: async () => input.endsWith('/agent/health') ? { enabled: true } : input.endsWith('/agent/connection') ? { available: true, model: 'test-model' } : input.includes('/agent/skills?') ? page([]) : input.includes('/audit-tasks?') ? { items: [] } : { revision: 1 } }
  }))
  const router = createMemoryRouter([{ path: '*', element: <AgentWorkbench /> }], { initialEntries: ['/'] })
  mount(<RouterProvider router={router} />)
  const input = await screen.findByRole('textbox', { name: '审计目标或问题' })
  fireEvent.change(input, { target: { value: '帮我规划付款核对' } })
  fireEvent.click(screen.getByRole('button', { name: '任务详情' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Agent 技能' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Agent 技能' }))
  // Ant Design gives simultaneously mounted overlays the same test-only title
  // ID; find the active dialog and assert its own heading instead.
  const dialog = (await screen.findAllByRole('dialog')).find(item => item.querySelector('.ant-drawer-title')?.textContent === 'Agent 技能')!
  expect(within(dialog).getByText('Agent 技能', { selector: '.ant-drawer-title' })).toBeInTheDocument()
  expect(input).toHaveValue('帮我规划付款核对')
  expect(writes).toHaveLength(0)
  expect(router.state.location.search).toBe('')
  await waitFor(() => expect(urls.some(url => url.includes('/agent/skills?') && !url.includes('workspace_id='))).toBe(true))
})

test('settings skill catalog browsing requires neither a project nor a business write', async () => {
  const urls: string[] = [], writes: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    urls.push(input); if (init?.method) writes.push(input)
    return { ok: true, json: async () => input.endsWith('/health') ? { enabled: true } : input.includes('/skills?') ? page([skill]) : { configured: true, available: true, model: 'local' } }
  }))
  mount(<MemoryRouter><PlatformSettings /></MemoryRouter>)
  const button = await screen.findByRole('button', { name: '浏览技能目录' })
  await waitFor(() => expect(button).toBeEnabled()); fireEvent.click(button)
  expect(await screen.findByLabelText('可用技能 payment-reconciliation')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '激活', exact: true })).toBeDisabled()
  expect(urls.some(url => url.includes('/skills?') && !url.includes('workspace_id='))).toBe(true)
  expect(writes).toHaveLength(0)
})
