import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Segmented, Select, Space, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import './classification.css'
import { useNativeWindow } from '../catalog/nativeWindow'

const { Title, Text } = Typography
type Project = { project_id: string; name: string; link_id: string | null; role: string | null;
  labels: { label_id: string; display_name: string }[] }
type AssetSet = { asset_set_id: string; revision: number;
  items: { item_id: string; asset_id: string }[];
  members: { asset_id: string; original_name: string }[] }
type Handoff = { handoff_id: string; link_id: string; state: string; availability: string;
  items: { item_id: string; name: string; label_id: string | null; status: string }[];
  processing: { model_id: string; device: string; platform_job_id: string | null } | null }
type Models = { items: { model_id: string; name: string }[] }
type PlatformJob = { status: string; stage: string;
  counts: { total: number | null; succeeded: number; failed: number; cancelled: number; pending: number } }

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
  const handoffId = params.get('handoff')
  const client = useQueryClient()
  const projectId = params.get('project') || undefined
  const [newName, setNewName] = useState('')
  const [categories, setCategories] = useState(['', ''])
  const native = useNativeWindow(`${workspaceId}:${projectId}`)
  const [labels, setLabels] = useState<Record<string, string | null>>({})
  const [modelId, setModelId] = useState<string>()
  const [device, setDevice] = useState<'cpu' | 'cuda'>('cpu')
  const [publishedBundle, setPublishedBundle] = useState<string | null>(null)
  useEffect(() => { setLabels({}); setModelId(undefined); setPublishedBundle(null) }, [workspaceId, projectId])
  const base = workspaceId ? `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/classification` : ''
  const handoffBase = `${base}/handoffs`
  const chooseProject = (id: string) => {
    setLabels({}); setModelId(undefined)
    const next = new URLSearchParams(params)
    next.set('project', id)
    next.delete('handoff')
    setParams(next)
  }
  const projects = useQuery({ queryKey: ['classification-projects', workspaceId], enabled: !!workspaceId,
    queryFn: ({ signal }) => json<{ items: Project[] }>(`${base}/projects`, { signal }) })
  const assetSet = useQuery({ queryKey: ['classification-asset-set', workspaceId, setId],
    enabled: !!workspaceId && !!setId,
    queryFn: ({ signal }) => json<AssetSet>(`/api/platform/v1/workspaces/${encodeURIComponent(workspaceId || '')}/assets/sets/${encodeURIComponent(setId || '')}`, { signal }) })
  const handoff = useQuery({ queryKey: ['classification-handoff', workspaceId, handoffId], enabled: !!workspaceId && !!handoffId,
    queryFn: ({ signal }) => json<Handoff>(`${handoffBase}/${encodeURIComponent(handoffId || '')}`, { signal }),
    refetchInterval: query => query.state.data?.state === 'completed' ? false : 5000 })
  const models = useQuery({ queryKey: ['classification-models', workspaceId, handoffId],
    enabled: !!workspaceId && !!handoffId && handoff.data?.state === 'completed',
    queryFn: ({ signal }) => json<Models>(`${handoffBase}/${encodeURIComponent(handoffId || '')}/models`, { signal }) })
  const platformJobId = handoff.data?.processing?.platform_job_id
  const terminal = ['succeeded', 'partial', 'failed', 'cancelled']
  const job = useQuery({ queryKey: ['classification-job', workspaceId, platformJobId],
    enabled: !!workspaceId && !!platformJobId,
    queryFn: ({ signal }) => json<PlatformJob>(`/api/platform/v1/workspaces/${encodeURIComponent(workspaceId || '')}/jobs/${encodeURIComponent(platformJobId || '')}`, { signal }),
    refetchInterval: query => terminal.includes(query.state.data?.status || '') ? false : 5000 })
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
  const begin = useMutation({ mutationFn: () => json<Handoff>(handoffBase, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ link_id: chosen?.link_id, asset_set_id: assetSet.data?.asset_set_id,
      asset_set_revision: assetSet.data?.revision,
      labels: Object.fromEntries(assetSet.data?.items.map(item => [item.item_id, labels[item.item_id] || null]) || []) }),
  }), onSuccess: result => {
    const next = new URLSearchParams(params)
    next.set('handoff', result.handoff_id)
    setParams(next)
    client.setQueryData(['classification-handoff', workspaceId, result.handoff_id], result)
  } })
  const resume = useMutation({ mutationFn: () => json<Handoff>(`${handoffBase}/${encodeURIComponent(handoffId || '')}/resume`, { method: 'POST' }),
    onSuccess: result => client.setQueryData(['classification-handoff', workspaceId, result.handoff_id], result) })
  const infer = useMutation({ mutationFn: () => json<{ handoff: Handoff }>(`${handoffBase}/${encodeURIComponent(handoffId || '')}/jobs`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ model_id: handoff.data?.processing?.model_id || modelId,
      device: handoff.data?.processing?.device || device }),
  }), onSuccess: result => client.setQueryData(['classification-handoff', workspaceId, result.handoff.handoff_id], result.handoff) })
  const publish = useMutation({ mutationFn: () => json<{ bundle_id: string }>(
    `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId || '')}/artifacts/classification-exports`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ handoff_id: handoffId }),
    }), onSuccess: result => {
      setPublishedBundle(result.bundle_id)
      void client.invalidateQueries({ queryKey: ['artifact-bundles', workspaceId] })
    } })
  const openNative = () => {
    if (!chosen?.link_id) return
    native.open(async signal => {
      const result = await json<{ url: string }>(`${base}/links/${encodeURIComponent(chosen.link_id!)}/navigation`, { method: 'POST', signal })
      return { url: result.url, kind: 'ticket' }
    })
  }
  const chosen = projects.data?.items.find(item => item.project_id === projectId)
  const validCategories = categories.length >= 2 && categories.every(item => item.trim())
    && new Set(categories.map(item => item.trim().toLocaleLowerCase())).size === categories.length
  const supported = assetSet.data?.members.filter(item => /\.(jpe?g|png|webp|bmp)$/i.test(item.original_name)) || []

  return <div className="classification-workflow"><div className="page-heading"><Text className="eyebrow">CLEARCLASSIFY / 图像分类与复核</Text>
    <Title level={1}>ClearClassify</Title></div>
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
        {chosen?.link_id && <Button className="classification-open" loading={native.opening} onClick={openNative}>打开原生项目</Button>}
        {native.error && <Alert type="error" title={native.error} className="classification-notice" />}
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
      {assetSet.data && <section className="panel"><div className="section-head"><Title level={3}>资料交接</Title>
        <Text type="secondary">{assetSet.data.members.length} 份资料</Text></div>
        <div className="classification-import-list">{assetSet.data.items.map((item, index) => <div key={item.item_id}>
          <span>{assetSet.data.members[index]?.original_name}</span>
          <Select aria-label={`${assetSet.data.members[index]?.original_name} 类别`} placeholder="未标注"
            value={handoff.data?.items.find(record => record.item_id === item.item_id)?.label_id || labels[item.item_id] || undefined}
            allowClear disabled={!!handoffId}
            onChange={value => setLabels(old => ({ ...old, [item.item_id]: value || null }))}
            options={chosen?.labels.map(label => ({ value: label.label_id, label: label.display_name }))} />
        </div>)}</div>
        <Button type="primary" disabled={!chosen?.link_id || supported.length !== assetSet.data.members.length || !!handoffId}
          loading={begin.isPending} onClick={() => begin.mutate()}>导入到当前项目</Button>
        {begin.isError && <Alert type="error" title={begin.error.message} className="classification-notice" />}
        {handoff.isError && <Alert type="error" title="交接状态无法读取" action={<Button onClick={() => void handoff.refetch()}>重试</Button>} className="classification-notice" />}
        {handoff.data && chosen?.link_id && handoff.data.link_id === chosen.link_id && <div className="classification-handoff-state">
          <Text strong>{handoff.data.state === 'completed' ? '导入完成' : handoff.data.availability === 'unreachable' ? '状态待核实' : '部分资料待导入'}</Text>
          <ul>{handoff.data.items.map(item => <li key={item.item_id}><span>{item.name}</span>
            <Text type="secondary">{item.status === 'imported' ? '已导入' : item.status === 'pending' ? '待导入' : '待核实'}</Text></li>)}</ul>
          {handoff.data.state !== 'completed' && <Button loading={resume.isPending} onClick={() => resume.mutate()}>核实并继续</Button>}
          {resume.isError && <Alert type="error" title={resume.error.message} className="classification-notice" />}
          {handoff.data.state === 'completed' && <div className="classification-inference">
            {models.isError && <Alert type="warning" title="模型列表暂不可用" action={<Button onClick={() => void models.refetch()}>重试</Button>} />}
            {models.data?.items.length === 0 && <Text type="secondary">当前项目暂无可用模型。</Text>}
            {!!models.data?.items.length && <Space wrap><Select aria-label="分类模型" placeholder="选择已有模型"
              style={{ minWidth: 210 }} disabled={!!handoff.data.processing}
              value={handoff.data.processing?.model_id || modelId} onChange={setModelId}
              options={models.data.items.map(item => ({ value: item.model_id, label: item.name }))} />
              <Segmented aria-label="运行设备" disabled={!!handoff.data.processing}
                value={device} onChange={value => setDevice(value as 'cpu' | 'cuda')}
                options={[{ label: 'CPU', value: 'cpu' }, { label: 'GPU', value: 'cuda' }]} />
              <Button type="primary" disabled={!(handoff.data.processing?.model_id || modelId) || !!handoff.data.processing?.platform_job_id}
                loading={infer.isPending} onClick={() => infer.mutate()}>
                {handoff.data.processing ? '核实原处理请求' : '提交分类'}</Button></Space>}
            {handoff.data.processing?.platform_job_id && <div className="classification-notice"><Text>任务已登记</Text>
              <Link to={`/jobs?workspace=${encodeURIComponent(workspaceId)}`}>查看任务中心</Link></div>}
            {job.data && <div className="classification-notice"><Text>
              {job.data.stage || '分类任务'} · 完成 {job.data.counts.succeeded} · 失败 {job.data.counts.failed} · 未处理 {job.data.counts.pending}
            </Text></div>}
            {job.data && terminal.includes(job.data.status) && <div className="classification-notice">
              <Button loading={publish.isPending} onClick={() => publish.mutate()}>归集成果</Button>
              {publishedBundle && <Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}&bundle=${encodeURIComponent(publishedBundle)}`}>查看成果</Link>}
            </div>}
            {publish.isError && <Alert type="error" title={publish.error.message} className="classification-notice" />}
            {infer.isError && <Alert type="error" title={infer.error.message} className="classification-notice" />}
          </div>}
        </div>}
      </section>}
    </>}
  </div>
}
