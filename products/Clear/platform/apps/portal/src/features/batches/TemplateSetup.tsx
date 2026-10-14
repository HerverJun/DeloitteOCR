import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Input, Select, Space, Typography } from 'antd'
import { Link } from 'react-router-dom'
import { post, request, scenarioNames, type Scenario, type Template } from './api'
import { FirstDataSetup } from './FirstDataSetup'

const { Text, Paragraph } = Typography
type Project = { project_id: string; name: string; link_id: string | null }
type Options = { models?: { id: string; name: string }[]; recognition?: boolean; reason?: string; engines?: { id: string; name: string }[] }

export function TemplateSetup({ workspaceId, onSaved }: { workspaceId: string; onSaved: (item: Template) => void }) {
  const client = useQueryClient()
  const [scenario, setScenario] = useState<Scenario>('data_compare')
  const [name, setName] = useState('')
  const [sessionId, setSessionId] = useState<string>()
  const [projectId, setProjectId] = useState<string>()
  const [modelId, setModelId] = useState<string>()
  const [engines, setEngines] = useState<string[]>([])
  const [device, setDevice] = useState('cpu')
  const nativeBase = `/workspaces/${encodeURIComponent(workspaceId)}/${scenario === 'ocr' ? 'ocr' : 'classification'}`
  const sessions = useQuery({ queryKey: ['data-sessions'], enabled: scenario === 'data_compare',
    queryFn: ({ signal }) => request<{ items: { id: string; name: string }[] }>('/data/sessions', { signal }) })
  const projects = useQuery({ queryKey: ['template-projects', workspaceId, scenario], enabled: scenario !== 'data_compare',
    queryFn: ({ signal }) => request<{ items: Project[] }>(`${nativeBase}/projects`, { signal }) })
  const chosen = projects.data?.items.find(item => item.project_id === projectId)
  const options = useQuery({ queryKey: ['template-native-options', workspaceId, scenario, chosen?.link_id], enabled: scenario !== 'data_compare' && !!chosen?.link_id,
    queryFn: ({ signal }) => request<Options>(`/batch-templates/options?${new URLSearchParams({ workspace_id: workspaceId, scenario, link_id: chosen!.link_id! })}`, { signal }) })
  const link = useMutation({ mutationFn: () => post<Project>(`${nativeBase}/links`, { project_id: projectId }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ['template-projects', workspaceId, scenario] }) })
  const save = useMutation({ mutationFn: () => {
    const common = { schema_version: '1.0', workspace_id: workspaceId, name: name.trim() }
    if (scenario === 'data_compare') return post<Template>('/batch-templates/from-data-session', { ...common, session_id: sessionId })
    return post<Template>('/batch-templates', { ...common, scenario, config: scenario === 'classification'
      ? { link_id: chosen!.link_id, model_id: modelId, device }
      : { link_id: chosen!.link_id, engines } })
  }, onSuccess: item => { onSaved(item); setName(''); void client.invalidateQueries({ queryKey: ['batch-templates', workspaceId] }) } })
  const valid = name.trim() && (scenario === 'data_compare' ? sessionId : chosen?.link_id && (scenario === 'classification' ? modelId : options.data?.recognition && engines.length))
  return <details className="batch-setup"><summary>首次配置或新增模板 <span>确认一次，后续批次复用</span></summary>
    <Paragraph>模板保存当前规则及关联的固定版本。调整规则时另存一个模板，已有批次继续使用原版本。</Paragraph>
    <div className="batch-template-fields"><label>处理场景<Select aria-label="模板处理场景" value={scenario} options={Object.entries(scenarioNames).map(([value, label]) => ({ value, label }))}
      onChange={value => { setScenario(value); setProjectId(undefined); setModelId(undefined); setEngines([]); save.reset() }} /></label>
      <label>模板名称<Input aria-label="处理模板名称" maxLength={100} value={name} onChange={event => setName(event.target.value)} placeholder="例如：月度对账" /></label>
      {scenario === 'data_compare' ? <label>已确认的表格作业<Select aria-label="模板来源表格作业" placeholder="选择已完成比对的作业" value={sessionId} onChange={setSessionId}
        loading={sessions.isPending} options={sessions.data?.items.map(item => ({ value: item.id, label: item.name }))} /></label> : <>
        <label>原生项目<Select aria-label="模板原生项目" placeholder="选择已有项目" value={projectId} loading={projects.isPending}
          onChange={value => { setProjectId(value); setModelId(undefined); setEngines([]) }} options={projects.data?.items.map(item => ({ value: item.project_id, label: `${item.name}${item.link_id ? ' · 已关联' : ''}` }))} /></label>
        {chosen && !chosen.link_id && <Button loading={link.isPending} onClick={() => link.mutate()}>关联到当前工作空间</Button>}
        {scenario === 'classification' && chosen?.link_id && <><label>固定模型<Select aria-label="模板分类模型" placeholder="选择已有模型" value={modelId} loading={options.isPending} onChange={setModelId}
          options={options.data?.models?.map(item => ({ value: item.id, label: item.name }))} /></label>
          <label>运行设备<Select aria-label="模板运行设备" value={device} onChange={setDevice} options={[{ value: 'cpu', label: 'CPU' }, { value: 'cuda', label: 'GPU' }]} /></label></>}
        {scenario === 'ocr' && chosen?.link_id && options.data?.recognition && <label>识别引擎<Select aria-label="模板识别引擎" mode="multiple" placeholder="选择已有引擎" value={engines} onChange={setEngines}
          options={options.data.engines?.map(item => ({ value: item.id, label: item.name }))} /></label>}
      </>}
    </div>
    {scenario === 'data_compare' ? <Paragraph type="secondary">先在 <Link to={`/apps/data?workspace=${encodeURIComponent(workspaceId)}`}>数据工作台</Link>确认输入结构、清洗步骤、匹配键和比对政策，再保存模板。系统按明确列名复用，不按列位置猜测。</Paragraph> :
      <Paragraph type="secondary">项目、模型训练和高级编辑仍可在 <Link to={`/apps/${scenario}/workflow?workspace=${encodeURIComponent(workspaceId)}`}>原生工作流程</Link>维护。</Paragraph>}
    {scenario === 'classification' && <Alert type="info" title="自动接受保持关闭" description="执行分类后保留原始模型建议，人的确认单独记录。" />}
    {scenario === 'ocr' && options.data && !options.data.recognition && <Alert type="warning" title="当前能力不足，不能自动识别" description={options.data.reason || '当前实例仅支持已有结果校对。请进入原生工作台处理已有结果。'} />}
    {[sessions.error, projects.error, options.error, link.error, save.error].filter(Boolean).map((error, index) => <Alert key={index} type="error" title={(error as Error).message} />)}
    {scenario === 'data_compare' ? <FirstDataSetup workspaceId={workspaceId} name={name} existingSessionId={sessionId} onSaved={item => { onSaved(item); setName('') }} /> :
      <Space><Button type="primary" disabled={!valid} loading={save.isPending} onClick={() => save.mutate()}>确认并保存模板</Button><Text type="secondary">保存后可直接选择新资料启动</Text></Space>}
  </details>
}
