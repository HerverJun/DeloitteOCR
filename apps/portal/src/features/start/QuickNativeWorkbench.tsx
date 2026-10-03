import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Select, Space, Typography } from 'antd'
import { Link } from 'react-router-dom'
import { useWorkspaceId, useWorkspaces } from '../../App'
import { applicationDescriptions, brandName } from '../../clear-ui/theme'
import { useNativeWindow } from '../catalog/nativeWindow'

const { Title, Text, Paragraph } = Typography
type NativeProject = { project_id: string; name: string; link_id: string | null }
type CreateIntent = { request_id: string; name: string; categories?: string[] }
async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) throw new Error('工具连接暂不可用，请刷新核对后重试。')
  return response.json()
}

export function QuickNativeWorkbench({ appId }: { appId: 'ocr' | 'classification' }) {
  const workspaceId = useWorkspaceId()
  return <><div className="page-heading"><Text className="eyebrow">CLEAR / PROCESS</Text><Title level={1}>{brandName(appId)}</Title>
    <Paragraph>{applicationDescriptions[appId].purpose} · 在工具内导入资料、处理并下载输出。</Paragraph></div>
    {workspaceId ? <NativeProject key={`${workspaceId}:${appId}`} workspaceId={workspaceId} appId={appId} /> : <Empty description="先选择项目" />}</>
}

function NativeProject({ workspaceId, appId }: { workspaceId: string; appId: 'ocr' | 'classification' }) {
  const workspaces = useWorkspaces(), client = useQueryClient()
  const base = `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/${appId}`
  const queryKey = [`${appId}-projects`, workspaceId]
  const projects = useQuery({ queryKey, queryFn: ({ signal }) => api<{ items: NativeProject[] }>(`${base}/projects`, { signal }) })
  const linked = projects.data?.items.filter(item => item.link_id) || []
  const [chosenId, setChosenId] = useState(''), [categories, setCategories] = useState('')
  const pendingKey = `clear-quick-native-create:${appId}:${workspaceId}`
  const [pending, setPending] = useState<CreateIntent | null>(() => {
    try { const value = JSON.parse(localStorage.getItem(pendingKey) || 'null'); return value?.request_id ? value : null } catch { return null }
  })
  const [legacyName, setLegacyName] = useState(() => {
    const value = localStorage.getItem(pendingKey) || ''
    try { return JSON.parse(value)?.request_id ? '' : value } catch { return value }
  })
  const chosen = linked.find(item => item.project_id === chosenId) || linked[0]
  const projectName = workspaces.data?.find(item => item.workspace_id === workspaceId)?.name || ''
  const categoryNames = categories.split(/\r?\n/).map(value => value.trim()).filter(Boolean)
  const categoriesValid = categoryNames.length >= 2 && categoryNames.length <= 1000 && new Set(categoryNames.map(value => value.toLowerCase())).size === categoryNames.length && categoryNames.every(value => value.length <= 60)
  const native = useNativeWindow(`${workspaceId}:${appId}`)
  useEffect(() => {
    if (legacyName && linked.some(item => item.name === legacyName)) {
      localStorage.removeItem(pendingKey); setLegacyName('')
    }
  }, [projects.data, legacyName, pendingKey])
  const open = () => native.open(async signal => {
    // Re-read before creating. A reload or lost response must not create a second job.
    const latest = await api<{ items: NativeProject[] }>(`${base}/projects`, { signal })
    let project = !pending && chosen ? latest.items.find(item => item.link_id && item.project_id === chosen.project_id)
      : latest.items.find(item => item.link_id)
    if (pending) project = undefined
    if (!pending && chosen && !project) throw new Error('所选工具作业已变化，请刷新后继续。')
    if (!project) {
      if (legacyName) throw new Error('旧创建请求需要先在工具内核对。')
      const intent = pending || { request_id: `n${crypto.randomUUID().replaceAll('-', '')}`, name: projectName.slice(0, 100),
        ...(appId === 'classification' ? { categories: categoryNames } : {}) }
      localStorage.setItem(pendingKey, JSON.stringify(intent)); setPending(intent)
      project = await api<NativeProject>(`${base}/projects`, { method: 'POST', signal,
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(intent) })
      if (!project.link_id) throw new Error('工具作业尚未关联，请刷新核对。')
      localStorage.removeItem(pendingKey)
      setPending(null)
      // Keep the new project stable while the reserved window is navigating.
      client.setQueryData(queryKey, { items: [...latest.items, project] })
    }
    const result = await api<{ url: string }>(`${base}/links/${encodeURIComponent(project.link_id!)}/quick-navigation`, { method: 'POST', signal })
    return { url: result.url, kind: 'ticket' }
  })
  return <section className="panel"><div className="section-head"><Title level={3}>{projectName || '当前项目'}</Title>
    <Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}`}>查看项目输出</Link></div>
    {projects.isError && <Alert type="warning" title="工具尚未连接" action={<Link to={`/apps/${appId}?workspace=${encodeURIComponent(workspaceId)}`}>检查并启动工具</Link>} />}
    {linked.length > 1 && <Select aria-label="继续工具作业" disabled={native.opening} value={chosen?.project_id} onChange={setChosenId}
      options={linked.map(item => ({ value: item.project_id, label: item.name }))} />}
    {!chosen && appId === 'classification' && !projects.isPending && !projects.isError && <div className="application-message">
      <Paragraph>首次使用时填写分类类别，每行一个。后续直接继续已有作业。</Paragraph>
      <Input.TextArea aria-label="分类类别" placeholder="例如：发票\n合同\n其他" rows={4} value={categories} onChange={event => setCategories(event.target.value)} /></div>}
    {pending && <Alert className="application-message" type="warning" title="上次创建尚未确认" description="点击继续确认，将恢复同一次创建并完成项目关联。" />}
    {legacyName && <Alert className="application-message" type="warning" title="旧创建请求需要核对" description="在工具内检查已有作业；选中原作业关联，或确认尚未创建后重新开始。" action={<Space wrap>
      <Link to={`/apps/${appId}?workspace=${encodeURIComponent(workspaceId)}`}>打开工具核对</Link>
      {projects.data?.items.filter(item => item.name === legacyName && !item.link_id).map(item => <Button key={item.project_id} onClick={() => {
        void api<NativeProject>(`${base}/links`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: item.project_id }) })
          .then(() => { localStorage.removeItem(pendingKey); setLegacyName(''); return projects.refetch() })
          .catch(() => void projects.refetch())
      }}>关联原作业 {item.project_id}</Button>)}
      <Button disabled={projects.isPending || projects.isError || native.opening} onClick={() => { localStorage.removeItem(pendingKey); setLegacyName('') }}>已核对未创建，重新开始</Button>
    </Space>} />}
    <Space wrap className="application-message"><Button type="primary" loading={native.opening}
      disabled={projects.isPending || projects.isError || !projectName || !!legacyName || (!pending && !chosen && appId === 'classification' && !categoriesValid)}
      onClick={open}>{pending ? '继续确认并打开' : chosen ? '打开并开始处理' : '开始处理'}</Button>
      <Button disabled={native.opening} onClick={() => void projects.refetch()}>刷新状态</Button>
      <Link to={`/?workspace=${encodeURIComponent(workspaceId)}&tool=${appId}`}>返回工具选择</Link></Space>
    {native.error && <Alert className="application-message" type="error" title={native.error} />}
  </section>
}
