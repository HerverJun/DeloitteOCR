import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Form, Input, Layout, Menu, Modal, Select, Skeleton, Space, Tag, Typography } from 'antd'
import { Link, Outlet, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import type { AppHealth, AppManifest, AppManifestPage, Workspace, WorkspacePage } from '../../../packages/contracts/types.generated'
import { ApplicationCard, ApplicationCards, RecentWorks } from './features/catalog/ApplicationCards'
import { DataApplicationCard } from './features/data/DataWorkbench'

const { Header, Sider, Content } = Layout
const { Title, Text, Paragraph } = Typography
type PlatformHealth = { service: string; ui: string; cpu: string; gpu: string; models: string }

async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal, headers: { Accept: 'application/json' } })
  if (!response.ok) throw new Error(`服务请求失败（${response.status}）`)
  return response.json() as Promise<T>
}

async function pages<T extends { items: unknown[]; next_cursor: string | null; snapshot_revision: number }>(path: string, signal: AbortSignal): Promise<T['items']> {
  const items: T['items'] = []
  let cursor: string | null = null
  let revision: number | null = null
  do {
    const params = new URLSearchParams({ limit: '200' })
    if (cursor !== null && revision !== null) { params.set('cursor', cursor); params.set('snapshot_revision', String(revision)) }
    const page: T = await read<T>(`${path}?${params}`, signal)
    if (revision !== null && revision !== page.snapshot_revision) throw new Error('列表已更新，请重试')
    revision = page.snapshot_revision
    items.push(...page.items)
    cursor = page.next_cursor
  } while (cursor !== null)
  return items
}

function useWorkspaceId() { const [params] = useSearchParams(); return params.get('workspace') || null }
function useWorkspaces() { return useQuery({ queryKey: ['workspaces'], queryFn: ({ signal }) => pages<WorkspacePage>('/api/platform/v1/workspaces', signal) as Promise<Workspace[]> }) }
function useApps(workspaceId: string | null) { return useQuery({ queryKey: ['apps', workspaceId], queryFn: ({ signal }) => pages<AppManifestPage>('/api/platform/v1/apps', signal) as Promise<AppManifest[]> }) }

function ScopeLink({ to, children }: { to: string; children: React.ReactNode }) {
  const workspaceId = useWorkspaceId()
  const path = to.split('?')[0]
  return <Link to={`${path}${workspaceId ? `?workspace=${encodeURIComponent(workspaceId)}` : ''}`}>{children}</Link>
}

const nav = [
  { key: '/', label: '首页' }, { key: '/workspaces', label: '工作空间' },
  { key: '/apps', label: '应用' }, { key: '/assets', label: '资料' },
  { key: '/jobs', label: '任务' }, { key: '/artifacts', label: '成果' }, { key: '/settings', label: '设置' },
]

export function App() {
  const location = useLocation(), navigate = useNavigate(), workspaceId = useWorkspaceId()
  const workspaces = useWorkspaces()
  const selected = workspaces.data?.find(item => item.workspace_id === workspaceId)
  const active = nav.find(item => item.key !== '/' && location.pathname.startsWith(item.key))?.key || '/'
  const scoped = (path: string, id: string | null = workspaceId) => `${path}${id ? `?workspace=${encodeURIComponent(id)}` : ''}`
  return <Layout className="shell">
    <Sider className="sidebar" breakpoint="lg" collapsedWidth="0" width={226}>
      <Link className="brand" to={scoped('/')}><span className="brand-mark">W</span><span>作业平台<small>LOCAL WORKBENCH</small></span></Link>
      <Menu theme="dark" mode="inline" selectedKeys={[active]} items={nav.map(item => ({ key: item.key, label: item.label }))}
        onClick={({ key }) => navigate(scoped(key))} aria-label="主导航" />
      <div className="sidebar-foot">本机工作台 · v0.1</div>
    </Sider>
    <Layout><Header className="topbar"><span className="breadcrumb">作业平台 <span>/</span> {nav.find(item => item.key === active)?.label}</span>
      {workspaces.isError ? <Button size="small" onClick={() => workspaces.refetch()}>工作空间加载失败 · 重试</Button> :
        <Select aria-label="当前工作空间" className="workspace-select" value={selected?.workspace_id || undefined}
          placeholder={workspaces.isPending ? '读取工作空间…' : '未选择工作空间'} loading={workspaces.isPending}
          allowClear
          onChange={id => navigate(scoped(location.pathname, id ?? null))}
          options={workspaces.data?.map(item => ({ value: item.workspace_id, label: item.name }))} />}
    </Header><Content className="page">
      {workspaceId && workspaces.isPending ? <div role="status" aria-label="正在验证工作空间"><Skeleton active /></div> :
        workspaceId && workspaces.isError ? <Alert type="error" title="无法验证工作空间" action={<Button onClick={() => void workspaces.refetch()}>重试</Button>} /> :
        workspaceId && !selected ? <Alert type="warning" title="工作空间不存在或已失效" description="请选择已有工作空间，或清除当前上下文。"
          action={<Button onClick={() => navigate(scoped(location.pathname, null))}>清除工作空间</Button>} /> : <Outlet />}
    </Content></Layout>
  </Layout>
}

function QueryState({ pending, error, retry, children, empty }: {
  pending: boolean; error: boolean; retry: () => void; children: React.ReactNode; empty?: boolean
}) {
  if (pending) return <div role="status" aria-label="正在加载"><Skeleton active paragraph={{ rows: 3 }} /></div>
  if (error) return <Alert type="error" showIcon title="暂时无法取得服务数据" description="确认平台服务已启动后重试。" action={<Button onClick={retry}>重试</Button>} />
  if (empty) return <Empty description="目前没有记录" />
  return <>{children}</>
}

function PageHeading({ eyebrow, title, description }: { eyebrow: string; title: string; description: string }) {
  return <div className="page-heading"><Text className="eyebrow">{eyebrow}</Text><Title level={1}>{title}</Title><Paragraph>{description}</Paragraph></div>
}

export function Home() {
  const workspaceId = useWorkspaceId()
  const health = useQuery({ queryKey: ['health', workspaceId], queryFn: ({ signal }) => read<PlatformHealth>('/api/platform/v1/health', signal) })
  const apps = useApps(workspaceId)
  return <><PageHeading eyebrow="OVERVIEW / LOCAL" title="从这里开始作业" description="选择已有工作空间，或直接查看可用工具。各工具的数据与作业仍由自身管理。" />
    <div className="home-grid"><section className="panel"><div className="section-head"><Title level={3}>平台连接</Title><Text type="secondary">本机服务</Text></div>
      <QueryState pending={health.isPending} error={health.isError} retry={() => void health.refetch()}>
        <Tag color={health.data?.service === 'ready' ? 'success' : 'warning'}>{health.data?.service === 'ready' ? '已连接' : '不可用'}</Tag>
        <Text type="secondary">CPU、GPU 与模型状态由相应工具提供，此处不推断。</Text>
      </QueryState></section>
      <section className="panel"><div className="section-head"><Title level={3}>可用应用</Title><ScopeLink to="/apps">查看目录 →</ScopeLink></div>
        <QueryState pending={apps.isPending} error={apps.isError} retry={() => void apps.refetch()} empty={apps.data?.length === 0}>
          <div className="plain-list"><div><ScopeLink to="/apps/data">数据清洗与比对</ScopeLink><Text type="secondary">Excel / CSV / Parquet</Text></div>{apps.data?.map(app => <div key={app.app_id}><ScopeLink to={`/apps/${app.app_id}`}>{app.display_name}</ScopeLink><Text type="secondary">{app.app_id}</Text></div>)}</div>
        </QueryState></section></div></>
}

export function Workspaces() {
  const list = useWorkspaces(), workspaceId = useWorkspaceId(), client = useQueryClient(), navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const [form] = Form.useForm<{ name: string }>()
  const create = useMutation({ mutationFn: async (name: string) => {
    const response = await fetch('/api/platform/v1/workspaces', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ schema_version: '1.0', name }) })
    if (!response.ok) throw new Error(`创建失败（${response.status}）`)
    return response.json() as Promise<Workspace>
  }, onSuccess: item => {
    // The confirmed server response is immediately visible while the list refreshes.
    client.setQueryData<Workspace[]>(['workspaces'], previous => [...(previous || []).filter(value => value.workspace_id !== item.workspace_id), item])
    setOpen(false); form.resetFields()
    navigate(`/workspaces?workspace=${encodeURIComponent(item.workspace_id)}`)
    void client.invalidateQueries({ queryKey: ['workspaces'] })
  } })
  return <><PageHeading eyebrow="WORKSPACES" title="工作空间" description="工作空间用于组织跨工具的项目关联；创建后可在顶部切换当前上下文。" />
    <div className="section-head"><Title level={3}>已有工作空间</Title><Button type="primary" onClick={() => setOpen(true)}>新建工作空间</Button></div>
    <QueryState pending={list.isPending} error={list.isError} retry={() => void list.refetch()} empty={list.data?.length === 0}>
      <div className="workspace-list">{list.data?.map(item => <Card key={item.workspace_id} className={item.workspace_id === workspaceId ? 'selected' : ''}>
        <Space orientation="vertical"><Link to={`/workspaces?workspace=${encodeURIComponent(item.workspace_id)}`}>{item.name}</Link>
          <Text type="secondary">{item.workspace_id}</Text></Space><Tag>{item.workspace_id === workspaceId ? '当前' : '可选择'}</Tag>
      </Card>)}</div>
    </QueryState>
    <Modal title="新建工作空间" open={open} onCancel={() => { setOpen(false); create.reset() }}
      onOk={() => void form.validateFields().then(values => create.mutate(values.name)).catch(() => undefined)}
      okText="创建" okButtonProps={{ loading: create.isPending }} cancelText="取消">
      <Form form={form} layout="vertical"><Form.Item label="名称" name="name" rules={[{ required: true, whitespace: true, message: '请输入名称' }, { max: 256 }]}>
        <Input autoFocus maxLength={256} placeholder="例如：季度资料整理" /></Form.Item></Form>
      {create.isError && <Alert type="error" showIcon title={create.error.message} />}
    </Modal></>
}

export function Applications() {
  const workspaceId = useWorkspaceId(), apps = useApps(workspaceId)
  return <><PageHeading eyebrow="APPLICATIONS" title="应用" description="选择工具查看安装与服务状态。无需先建立工作空间。" />
    <div className="app-grid" style={{ marginBottom: 16 }}><DataApplicationCard /></div>
    <QueryState pending={apps.isPending} error={apps.isError} retry={() => void apps.refetch()} empty={apps.data?.length === 0}>
      <ApplicationCards apps={apps.data || []} workspaceId={workspaceId} />
    </QueryState><RecentWorks workspaceId={workspaceId} /></>
}

export function Application() {
  const { appId } = useParams(), workspaceId = useWorkspaceId()
  const validId = !!appId && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(appId)
  const safeId = encodeURIComponent(appId || '')
  const info = useQuery({ queryKey: ['app', workspaceId, appId], enabled: validId, queryFn: ({ signal }) => read<AppManifest>(`/api/platform/v1/apps/${safeId}`, signal) })
  const health = useQuery({ queryKey: ['app-health', workspaceId, appId], enabled: validId, queryFn: ({ signal }) => read<AppHealth>(`/api/platform/v1/apps/${safeId}/health`, signal) })
  if (!validId) return <Missing />
  return <><PageHeading eyebrow="APPLICATION / STATUS" title={info.data?.display_name || '应用状态'} description="核实本机实例后打开工具。无需先建立工作空间。" />
    <QueryState pending={info.isPending || health.isPending} error={info.isError || health.isError}
      retry={() => { void info.refetch(); void health.refetch() }}>
      <div className="panel"><div className="section-head"><Title level={3}>{info.data?.display_name}</Title><Tag>{info.data?.app_version}</Tag></div>
        <div className="status-list">{(['service', 'ui', 'cpu', 'gpu', 'models'] as const).map(key => <div key={key}>
          <Text>{({ service: '服务', ui: '界面', cpu: 'CPU', gpu: 'GPU', models: '模型' })[key]}</Text>
          <Tag color={health.data?.[key].state === 'ready' ? 'success' : 'default'}>{health.data?.[key].state || 'unknown'}</Tag>
          <Text type="secondary">{health.data?.[key].reason}</Text></div>)}</div></div>
      {info.data && <ApplicationCard key={info.data.app_id} app={info.data} workspaceId={workspaceId} />}
    </QueryState><div className="back-link"><ScopeLink to="/apps">← 返回应用目录</ScopeLink></div></>
}

export function Placeholder({ title, description }: { title: string; description: string }) {
  return <><PageHeading eyebrow="WORKBENCH" title={title} description={description} />
    <div className="empty-panel"><Empty description={`${title}功能尚未开放`} /><ScopeLink to="/apps">查看可用应用 →</ScopeLink></div></>
}
export function Missing() { return <><PageHeading eyebrow="NOT FOUND" title="页面不存在" description="这个地址不属于当前门户。" /><ScopeLink to="/">返回首页 →</ScopeLink></> }
