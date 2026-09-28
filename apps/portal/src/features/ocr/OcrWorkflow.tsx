import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Select, Space, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import './ocr.css'

const { Title, Text } = Typography
type Project = { project_id: string; name: string; link_id: string | null; role: string | null }
type AssetSet = { asset_set_id: string; revision: number; members: { asset_id: string; original_name: string }[] }
type Handoff = { handoff_id: string; link_id: string; state: string; availability: string;
  items: { item_id: string; name: string; status: string; page_count: number | null }[];
  processing: { engines: string[]; platform_job_id: string | null; request_state: string } | null }
type OcrCapabilities = { recognition: boolean; reason: string | null; engines: { id: string; name: string }[] }

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(typeof body.detail === 'string' ? body.detail : `OCR 请求失败（${response.status}）`)
  }
  return response.json() as Promise<T>
}

export function OcrWorkflow() {
  const [params, setParams] = useSearchParams()
  const workspaceId = params.get('workspace')
  const setId = params.get('set')
  const handoffId = params.get('handoff')
  const client = useQueryClient()
  const [projectId, setProjectId] = useState<string | undefined>(params.get('project') || undefined)
  const [newName, setNewName] = useState('')
  const [engines, setEngines] = useState<string[]>([])
  const [opening, setOpening] = useState(false)
  const [openError, setOpenError] = useState('')
  const chooseProject = (id: string) => {
    setProjectId(id)
    const next = new URLSearchParams(params)
    next.set('project', id)
    next.delete('handoff')
    setParams(next)
  }
  const base = workspaceId ? `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/ocr` : ''
  const projects = useQuery({ queryKey: ['ocr-projects', workspaceId], enabled: !!workspaceId,
    queryFn: ({ signal }) => json<{ items: Project[] }>(`${base}/projects`, { signal }) })
  const assetSet = useQuery({ queryKey: ['ocr-asset-set', workspaceId, setId], enabled: !!workspaceId && !!setId,
    queryFn: ({ signal }) => json<AssetSet>(`/api/platform/v1/workspaces/${encodeURIComponent(workspaceId || '')}/assets/sets/${encodeURIComponent(setId || '')}`, { signal }) })
  const handoffBase = `${base}/handoffs`
  const handoff = useQuery({ queryKey: ['ocr-handoff', workspaceId, handoffId], enabled: !!workspaceId && !!handoffId,
    queryFn: ({ signal }) => json<Handoff>(`${handoffBase}/${encodeURIComponent(handoffId || '')}`, { signal }),
    refetchInterval: query => query.state.data?.state === 'completed' ? false : 5000 })
  const capabilities = useQuery({ queryKey: ['ocr-handoff-capabilities', workspaceId, handoffId],
    enabled: !!workspaceId && !!handoffId && handoff.data?.state === 'completed',
    queryFn: ({ signal }) => json<OcrCapabilities>(`${handoffBase}/${encodeURIComponent(handoffId || '')}/capabilities`, { signal }) })
  const link = useMutation({ mutationFn: (id: string) => json<Project>(`${base}/links`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: id }),
  }), onSuccess: result => { chooseProject(result.project_id); void client.invalidateQueries({ queryKey: ['ocr-projects', workspaceId] }) } })
  const create = useMutation({ mutationFn: (name: string) => json<Project>(`${base}/projects`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name }),
  }), onSuccess: result => { setNewName(''); chooseProject(result.project_id); void client.invalidateQueries({ queryKey: ['ocr-projects', workspaceId] }) } })
  const chosen = projects.data?.items.find(item => item.project_id === projectId)
  const unsupported = assetSet.data?.members.filter(item => !/\.(jpe?g|png|bmp|tiff?|webp|heic|heif|pdf)$/i.test(item.original_name)) || []
  const begin = useMutation({ mutationFn: () => json<Handoff>(handoffBase, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ link_id: chosen?.link_id, asset_set_id: assetSet.data?.asset_set_id,
      asset_set_revision: assetSet.data?.revision }),
  }), onSuccess: result => {
    const next = new URLSearchParams(params)
    next.set('handoff', result.handoff_id)
    setParams(next)
    client.setQueryData(['ocr-handoff', workspaceId, result.handoff_id], result)
  } })
  const resume = useMutation({ mutationFn: () => json<Handoff>(`${handoffBase}/${encodeURIComponent(handoffId || '')}/resume`, { method: 'POST' }),
    onSuccess: result => client.setQueryData(['ocr-handoff', workspaceId, result.handoff_id], result) })
  const submit = useMutation({ mutationFn: () => json<{ handoff: Handoff }>(`${handoffBase}/${encodeURIComponent(handoffId || '')}/tasks`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ engines: handoff.data?.processing?.engines || engines }),
  }), onSuccess: result => client.setQueryData(['ocr-handoff', workspaceId, result.handoff.handoff_id], result.handoff) })
  const publish = useMutation({ mutationFn: () => json<{ bundle_id: string }>(
    `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId || '')}/artifacts/ocr-exports`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ handoff_id: handoffId }),
    }) })
  const openNative = async () => {
    if (!chosen?.link_id) return
    const popup = window.open('', '_blank')
    if (!popup) { setOpenError('未能打开新窗口；请允许弹出窗口后重试。'); return }
    popup.opener = null
    setOpening(true); setOpenError('')
    try {
      const result = await json<{ url: string }>(`${base}/links/${encodeURIComponent(chosen.link_id)}/navigation`, { method: 'POST' })
      if (!/^http:\/\/127\.0\.0\.1:[1-9][0-9]{0,4}\/#platform_ticket=[A-Za-z0-9_-]{32,128}$/.test(result.url)) throw new Error('原生入口无效')
      popup.location.replace(result.url)
    } catch (failure) { popup.close(); setOpenError((failure as Error).message) }
    finally { setOpening(false) }
  }
  return <div className="ocr-workflow"><div className="page-heading"><Text className="eyebrow">OCR / WORKSPACE</Text>
    <Title level={1}>OCR 资料处理</Title></div>
    {!workspaceId ? <Empty description="先在顶部选择工作空间" /> : <>
      <section className="panel"><div className="section-head"><Title level={3}>输入资料</Title>
        <Link to={`/assets?workspace=${encodeURIComponent(workspaceId)}${setId ? `&set=${encodeURIComponent(setId)}` : ''}`}>返回资料库</Link></div>
        {assetSet.isError && <Alert type="error" title="资料集无法读取" action={<Button onClick={() => void assetSet.refetch()}>重试</Button>} />}
        {assetSet.data ? <><Text type="secondary">版本 {assetSet.data.revision} · {assetSet.data.members.length} 份</Text>
          <ul className="ocr-input-list">{assetSet.data.members.map(item => <li key={item.asset_id}>{item.original_name}</li>)}</ul></> :
          !setId && <Empty description="从资料库选择并固定输入集合" />}
      </section>
      <section className="panel"><div className="section-head"><Title level={3}>OCR 项目</Title>
        <Button onClick={() => void projects.refetch()}>刷新项目</Button></div>
        {projects.isError && <Alert type="warning" title="OCR 项目暂不可用"
          action={<Link to={`/apps/ocr?workspace=${encodeURIComponent(workspaceId)}`}>查看应用状态</Link>} />}
        <div className="ocr-project-controls"><Select aria-label="OCR 项目" placeholder="选择已有项目" value={projectId}
          loading={projects.isPending} onChange={chooseProject} options={projects.data?.items.map(item => ({
            value: item.project_id, label: `${item.name}${item.link_id ? ' · 已关联' : ''}`,
          }))} />
          <Button disabled={!chosen || !!chosen.link_id} loading={link.isPending}
            onClick={() => projectId && link.mutate(projectId)}>关联当前工作空间</Button></div>
        {link.isError && <Alert type="error" title={link.error.message} className="ocr-notice" />}
        <div className="ocr-project-controls"><Input aria-label="新 OCR 项目名称" placeholder="新项目名称" maxLength={120}
          value={newName} onChange={event => setNewName(event.target.value)} />
          <Button disabled={!newName.trim()} loading={create.isPending} onClick={() => create.mutate(newName.trim())}>新建并关联</Button></div>
        {create.isError && <Alert type="error" title={create.error.message} className="ocr-notice" />}
        {chosen?.link_id && <Alert type="success" title={`已关联：${chosen.name}`} className="ocr-notice" />}
        {chosen?.link_id && <Button className="ocr-open" loading={opening} onClick={() => void openNative()}>打开原生项目</Button>}
        {openError && <Alert type="error" title={openError} className="ocr-notice" />}
      </section>
      {assetSet.data && <section className="panel"><div className="section-head"><Title level={3}>资料交接</Title>
        <Text type="secondary">{assetSet.data.members.length} 份资料</Text></div>
        {unsupported.length > 0 && <Alert type="warning" title="当前导入入口只接收图片或 PDF"
          description={`需另行处理：${unsupported.map(item => item.original_name).join('、')}`} className="ocr-notice" />}
        <Button type="primary" disabled={!chosen?.link_id || unsupported.length > 0 || !!handoffId}
          loading={begin.isPending} onClick={() => begin.mutate()}>导入到当前项目</Button>
        {begin.isError && <Alert type="error" title={begin.error.message} className="ocr-notice" />}
        {handoff.isError && <Alert type="error" title="交接状态无法读取" action={<Button onClick={() => void handoff.refetch()}>重试</Button>} className="ocr-notice" />}
        {handoff.data && chosen?.link_id && handoff.data.link_id === chosen.link_id && <div className="ocr-handoff-state">
          <Text strong>{handoff.data.state === 'completed' ? '导入完成' : handoff.data.availability === 'unreachable' ? '状态待核实' : '部分资料待导入'}</Text>
          <ul>{handoff.data.items.map(item => <li key={item.item_id}><span>{item.name}</span>
            <Text type="secondary">{item.status === 'imported' ? item.page_count === null ? '已导入' : `${item.page_count} 页已登记` : item.status === 'pending' ? '待导入' : '待核实'}</Text></li>)}</ul>
          {handoff.data.state !== 'completed' && <Button loading={resume.isPending} onClick={() => resume.mutate()}>核实并继续</Button>}
          {resume.isError && <Alert type="error" title={resume.error.message} className="ocr-notice" />}
          {handoff.data.state === 'completed' && <div className="ocr-processing">
            {capabilities.isError && <Alert type="warning" title="识别能力暂不可读取" action={<Button onClick={() => void capabilities.refetch()}>重试</Button>} />}
            {capabilities.data && !capabilities.data.recognition && <Text type="secondary">
              {capabilities.data.reason === 'native_document_flow' ? 'PDF 已进入原生文档，请在原生 OCR 按页处理。' : '当前 OCR 实例仅提供审阅与导出。'}</Text>}
            {capabilities.data?.reason === 'native_document_flow' && <div className="ocr-notice">
              <Button loading={publish.isPending} onClick={() => publish.mutate()}>归集已处理的文档成果</Button>
              {publish.data && <Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}&bundle=${encodeURIComponent(publish.data.bundle_id)}`}>查看成果</Link>}
            </div>}
            {capabilities.data?.recognition && <Space wrap><Select mode="multiple" aria-label="识别引擎" placeholder="选择识别引擎"
              value={handoff.data.processing?.engines || engines} onChange={setEngines}
              disabled={!!handoff.data.processing} style={{ minWidth: 220 }}
              options={capabilities.data.engines.map(item => ({ value: item.id, label: item.name }))} />
              <Button type="primary" disabled={!(handoff.data.processing?.engines.length || engines.length) || !!handoff.data.processing?.platform_job_id}
                loading={submit.isPending} onClick={() => submit.mutate()}>
                {handoff.data.processing ? '核实原处理请求' : '提交识别'}</Button></Space>}
            {handoff.data.processing?.platform_job_id && <div className="ocr-notice"><Text>任务已登记</Text>
              <Link to={`/jobs?workspace=${encodeURIComponent(workspaceId)}`}>查看任务中心</Link>
              <Button loading={publish.isPending} onClick={() => publish.mutate()}>归集识别成果</Button>
              {publish.data && <Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}&bundle=${encodeURIComponent(publish.data.bundle_id)}`}>查看成果</Link>}</div>}
            {publish.isError && <Alert type="error" title={publish.error.message} className="ocr-notice" />}
            {submit.isError && <Alert type="error" title={submit.error.message} className="ocr-notice" />}
          </div>}
        </div>}
      </section>}
    </>}
  </div>
}
