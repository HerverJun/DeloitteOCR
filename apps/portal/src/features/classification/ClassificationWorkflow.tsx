import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Select, Space, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import './classification.css'

const { Title, Text } = Typography
type Project = { project_id: string; name: string; link_id: string | null; role: string | null;
  labels: { label_id: string; display_name: string }[] }
type AssetSet = { asset_set_id: string; revision: number;
  members: { asset_id: string; original_name: string }[] }

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : `分类请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

export function ClassificationWorkflow() {
  const [params, setParams] = useSearchParams()
  const workspaceId = params.get('workspace')
  const setId = params.get('set')
  const client = useQueryClient()
  const [projectId, setProjectId] = useState<string | undefined>(params.get('project') || undefined)
  const [newName, setNewName] = useState('')
  const [categories, setCategories] = useState(['', ''])
  const base = workspaceId ? `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/classification` : ''
  const chooseProject = (id: string) => {
    setProjectId(id)
    const next = new URLSearchParams(params)
    next.set('project', id)
    setParams(next)
  }
  const projects = useQuery({ queryKey: ['classification-projects', workspaceId], enabled: !!workspaceId,
    queryFn: ({ signal }) => json<{ items: Project[] }>(`${base}/projects`, { signal }) })
  const assetSet = useQuery({ queryKey: ['classification-asset-set', workspaceId, setId],
    enabled: !!workspaceId && !!setId,
    queryFn: ({ signal }) => json<AssetSet>(`/api/platform/v1/workspaces/${encodeURIComponent(workspaceId || '')}/assets/sets/${encodeURIComponent(setId || '')}`, { signal }) })
  const link = useMutation({ mutationFn: (id: string) => json<Project>(`${base}/links`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: id }),
  }), onSuccess: result => {
    chooseProject(result.project_id)
    void client.invalidateQueries({ queryKey: ['classification-projects', workspaceId] })
  } })
  const create = useMutation({ mutationFn: () => json<Project>(`${base}/projects`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: newName.trim(), categories: categories.map(item => item.trim()) }),
  }), onSuccess: result => {
    setNewName(''); setCategories(['', '']); chooseProject(result.project_id)
    void client.invalidateQueries({ queryKey: ['classification-projects', workspaceId] })
  } })
  const chosen = projects.data?.items.find(item => item.project_id === projectId)
  const validCategories = categories.length >= 2 && categories.every(item => item.trim())
    && new Set(categories.map(item => item.trim().toLocaleLowerCase())).size === categories.length
  const supported = assetSet.data?.members.filter(item => /\.(jpe?g|png|webp|bmp)$/i.test(item.original_name)) || []

  return <div className="classification-workflow"><div className="page-heading"><Text className="eyebrow">CLASSIFICATION / WORKSPACE</Text>
    <Title level={1}>图像分类</Title></div>
    {!workspaceId ? <Empty description="先在顶部选择工作空间" /> : <>
      <section className="panel"><div className="section-head"><Title level={3}>输入资料</Title>
        <Link to={`/assets?workspace=${encodeURIComponent(workspaceId)}${setId ? `&set=${encodeURIComponent(setId)}` : ''}`}>返回资料库</Link></div>
        {assetSet.isError && <Alert type="error" title="资料集无法读取" action={<Button onClick={() => void assetSet.refetch()}>重试</Button>} />}
        {assetSet.data ? <><Text type="secondary">版本 {assetSet.data.revision} · {assetSet.data.members.length} 份</Text>
          <ul className="classification-inputs">{assetSet.data.members.map(item => <li key={item.asset_id}>{item.original_name}</li>)}</ul>
          {supported.length !== assetSet.data.members.length && <Alert type="warning" title="分类只接收静态 JPG、PNG、WebP 或 BMP 图片" />}</> :
          !setId && <Empty description="从资料库选择并固定输入集合" />}
      </section>
      <section className="panel"><div className="section-head"><Title level={3}>分类项目</Title>
        <Button onClick={() => void projects.refetch()}>刷新项目</Button></div>
        {projects.isError && <Alert type="warning" title="分类项目暂不可用"
          action={<Link to={`/apps/classification?workspace=${encodeURIComponent(workspaceId)}`}>查看应用状态</Link>} />}
        <div className="classification-project-controls"><Select aria-label="分类项目" placeholder="选择已有项目"
          value={projectId} onChange={chooseProject} loading={projects.isPending}
          options={projects.data?.items.map(item => ({ value: item.project_id,
            label: `${item.name}${item.link_id ? ' · 已关联' : ''}` }))} />
          <Button disabled={!chosen || !!chosen.link_id} loading={link.isPending}
            onClick={() => projectId && link.mutate(projectId)}>关联当前工作空间</Button></div>
        {chosen && <div className="classification-labels"><Text type="secondary">项目类别</Text>
          <Space size={6} wrap>{chosen.labels.map(item => <Tag key={item.label_id}>{item.display_name}</Tag>)}</Space></div>}
        {link.isError && <Alert type="error" title={link.error.message} className="classification-notice" />}
        {chosen?.link_id && <Alert type="success" title={`已关联：${chosen.name}`} className="classification-notice" />}
      </section>
      <section className="panel"><div className="section-head"><Title level={3}>新建分类项目</Title></div>
        <div className="classification-create"><Input aria-label="新分类项目名称" placeholder="项目名称" maxLength={100}
          value={newName} onChange={event => setNewName(event.target.value)} />
          <div className="classification-category-list">{categories.map((item, index) => <div key={index}>
            <Input aria-label={`类别 ${index + 1}`} placeholder={`类别 ${index + 1}`} maxLength={60}
              value={item} onChange={event => setCategories(old => old.map((value, i) => i === index ? event.target.value : value))} />
            <Button aria-label={`删除类别 ${index + 1}`} disabled={categories.length <= 2}
              onClick={() => setCategories(old => old.filter((_, i) => i !== index))}>删除</Button>
          </div>)}</div>
          <Space><Button disabled={categories.length >= 50} onClick={() => setCategories(old => [...old, ''])}>添加类别</Button>
            <Button type="primary" disabled={!newName.trim() || !validCategories}
              loading={create.isPending} onClick={() => create.mutate()}>新建并关联</Button></Space></div>
        {create.isError && <Alert type="error" title={create.error.message} className="classification-notice" />}
      </section>
    </>}
  </div>
}
