import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Alert, Button, Form, Input, Select, Space, Table, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { useWorkspaceId, Workspaces } from '../../App'
import { assets, HttpError, newRequestId, stateNames, type Asset } from '../batches/api'
import { UploadQueue } from '../batches/UploadQueue'
import { navigationHref } from '../../clear-ui/navigation'
import { ComparisonResults } from './ComparisonResults'
import { ConnectionSettings, LayoutOptions, ProfileEditor, ProviderSelect } from './Configuration'
import { activeComparison, assetReference, comparisonsApi, defaultLayout, defaultProfile, inputError, moveAsset, profileError,
  type ComparisonCreate, type ComparisonProfile, type ComparisonRun, type LayoutParseConfig } from './api'
import './comparisons.css'

const { Title, Paragraph, Text } = Typography

function MaterialSelector({ label, items, selected, change, multiple = false, disabled = false }: { label: string; items: Asset[]; selected: string[]; change: (ids: string[]) => void; multiple?: boolean; disabled?: boolean }) {
  return <><Form layout="vertical"><Form.Item label={label}><Select aria-label={label} mode={multiple ? 'multiple' : undefined} showSearch optionFilterProp="label" placeholder={multiple ? '选择 PPTX 或按顺序选择图片' : '选择 PDF'}
    value={multiple ? selected : selected[0]} disabled={disabled} onChange={value => change(Array.isArray(value) ? value : [value])}
    options={items.map(item => ({ value: item.asset_id, label: `${item.original_name} · v${item.revision}` }))} /></Form.Item></Form>
    {multiple && selected.length > 1 && <ol className="comparison-image-order" aria-label="幻灯片图片顺序">{selected.map((id, index) => <li key={id}><span>{items.find(item => item.asset_id === id)?.original_name || id}</span>
      <Space><Button size="small" aria-label={`第 ${index + 1} 张上移`} disabled={index === 0} onClick={() => change(moveAsset(selected, index, -1))}>上移</Button>
        <Button size="small" aria-label={`第 ${index + 1} 张下移`} disabled={index === selected.length - 1} onClick={() => change(moveAsset(selected, index, 1))}>下移</Button></Space></li>)}</ol>}
  </>
}

function restoredRequest(workspace: string): ComparisonCreate | null {
  try { const value = JSON.parse(localStorage.getItem(`clear-comparison-request:${workspace}`) || 'null')
    return value && typeof value.request_id === 'string' && value.left?.assets?.length && value.right?.assets?.length && value.comparison_provider_id ? value : null
  } catch { return null }
}

export function ComparisonPanel({ workspace }: { workspace: string }) {
  const [params, setParams] = useSearchParams(), selected = params.get('comparison') || ''
  const revision = Number(params.get('comparison_revision')) || undefined
  const [title, setTitle] = useState('文档主题对比'), [left, setLeft] = useState<string[]>([]), [right, setRight] = useState<string[]>([])
  const [leftLayout, setLeftLayout] = useState<LayoutParseConfig>({ ...defaultLayout }), [rightLayout, setRightLayout] = useState<LayoutParseConfig>({ ...defaultLayout })
  const [profile, setProfile] = useState<ComparisonProfile>({ ...defaultProfile, fields: defaultProfile.fields.map(item => ({ ...item })) })
  const [savedProfile, setSavedProfile] = useState<ComparisonProfile | null>(null), [providerId, setProviderId] = useState<string | null>(null)
  const initializedProfile = useRef(false)
  const [aliasesValid, setAliasesValid] = useState(true)
  const [showConnections, setShowConnections] = useState(false), [uploadBusy, setUploadBusy] = useState(false), [notice, setNotice] = useState('')
  const [pending, setPending] = useState<ComparisonCreate | null>(() => restoredRequest(workspace))
  const [historyPage, setHistoryPage] = useState(1)
  const media = useQuery({ queryKey: ['comparison-assets', workspace], queryFn: ({ signal }) => assets(workspace, signal) })
  const connections = useQuery({ queryKey: ['comparison-connections', workspace], queryFn: () => comparisonsApi.connections(workspace) })
  const profiles = useQuery({ queryKey: ['comparison-profiles', workspace], queryFn: () => comparisonsApi.profiles(workspace) })
  const listing = useQuery({ queryKey: ['comparisons', workspace, historyPage], queryFn: () => comparisonsApi.list(workspace, (historyPage - 1) * 5, 5), refetchInterval: query => query.state.data?.items.some(item => activeComparison(item.status)) ? 2500 : false })
  const detail = useQuery({ queryKey: ['comparison', workspace, selected, revision], enabled: !!selected, queryFn: () => comparisonsApi.get(workspace, selected, revision), refetchInterval: query => !revision && query.state.data && activeComparison(query.state.data.status) ? 1500 : false })
  const latest = useQuery({ queryKey: ['comparison-latest', workspace, selected], enabled: !!selected && !!revision, queryFn: () => comparisonsApi.get(workspace, selected), refetchInterval: query => query.state.data && activeComparison(query.state.data.status) ? 1500 : false })
  const available = connections.data?.items || [], provider = available.find(item => item.id === providerId), mediaList = media.data || []
  const selectedLeft = left.map(id => mediaList.find(item => item.asset_id === id)).filter((item): item is Asset => !!item)
  const selectedRight = right.map(id => mediaList.find(item => item.asset_id === id)).filter((item): item is Asset => !!item)
  const materialError = left.length !== selectedLeft.length || right.length !== selectedRight.length ? '部分已选材料当前不可用，请刷新资料后重新选择。' : inputError(selectedLeft, selectedRight), fieldsError = profileError(profile)
  const profileDirty = !savedProfile || JSON.stringify(savedProfile) !== JSON.stringify(profile)
  const choose = (run: ComparisonRun) => setParams(previous => { const next = new URLSearchParams(previous); next.set('comparison', run.comparison_id); next.delete('comparison_revision'); return next })
  const refresh = () => { void listing.refetch(); if (selected) void detail.refetch() }
  useEffect(() => { if (!initializedProfile.current && profiles.data?.items.length) { const initial = profiles.data.items.find(item => item.id === 'project_progress') || profiles.data.items[0]; setProfile(initial); setSavedProfile(initial); initializedProfile.current = true } }, [profiles.data])
  useEffect(() => {
    const refreshProvider = (config: LayoutParseConfig) => { const latest = connections.data?.items.find(item => item.id === config.provider_id);
      return latest && config.provider_version !== latest.version ? { ...config, provider_version: latest.version, max_output_tokens: Math.min(config.max_output_tokens, latest.max_output_tokens) } : config }
    setLeftLayout(refreshProvider); setRightLayout(refreshProvider)
  }, [connections.data])
  const saveProfile = useMutation({ mutationFn: () => { if (fieldsError || !aliasesValid) throw new Error(fieldsError || '请修正项目别名 JSON。'); return comparisonsApi.saveProfile(workspace, profile, !savedProfile) }, onSuccess: saved => {
    setProfile(saved); setSavedProfile(saved); setNotice(`已保存比较配置「${saved.name}」v${saved.version}`); void profiles.refetch()
  } })
  const suggest = useMutation({ mutationFn: () => { if (!provider) throw new Error('请先选择比较模型。'); return comparisonsApi.suggest(workspace, profile.instructions, provider) }, onSuccess: suggested => {
    setProfile({ ...suggested, id: profile.id, version: profile.version }); setAliasesValid(true); setNotice('字段建议已生成，请检查并保存配置版本。')
  } })
  const persist = (payload: ComparisonCreate | null) => { setPending(payload); const key = `clear-comparison-request:${workspace}`; if (payload) localStorage.setItem(key, JSON.stringify(payload)); else localStorage.removeItem(key) }
  const create = useMutation({ mutationFn: async () => {
    if (pending) return comparisonsApi.create(workspace, pending)
    if (materialError) throw new Error(materialError)
    if (!provider || !savedProfile || profileDirty) throw new Error('请选择比较模型，并保存当前字段配置。')
    for (const config of [leftLayout, rightLayout]) if (config.mode === 'multimodal' && !config.provider_id) throw new Error('请为多模态解析选择连接。')
    const payload: ComparisonCreate = { request_id: newRequestId(), title: title.trim() || '文档主题对比', left: { assets: selectedLeft.map(assetReference), layout: leftLayout },
      right: { assets: selectedRight.map(assetReference), layout: rightLayout }, profile_id: savedProfile.id, profile_version: savedProfile.version,
      comparison_provider_id: provider.id, comparison_provider_version: provider.version }
    persist(payload)
    return comparisonsApi.create(workspace, payload)
  }, onSuccess: run => { persist(null); choose(run); setHistoryPage(1); void listing.refetch(); setNotice('任务已登记，逐页结果会自动保存。') },
  onError: error => { if (error instanceof HttpError && error.status >= 400 && error.status < 500 && ![408, 409, 429].includes(error.status)) persist(null) } })
  const parseReady = [leftLayout, rightLayout].every(config => config.mode === 'local_ocr' || available.some(item => item.id === config.provider_id && item.supports_vision))
  const nextStep = pending ? '继续将核实上次提交，不会创建重复任务。' : uploadBusy ? '正在上传材料，完成后在两侧选择本次要比较的文件。'
    : media.isPending || connections.isPending || profiles.isPending ? '正在读取材料与配置，请稍候。'
    : materialError || fieldsError || (!aliasesValid ? '请在比较要求中修正项目别名。' : '')
      || (profileDirty ? '比较要求有未保存的修改，请先保存配置版本。' : '')
      || (!provider ? '请选择用于比较两侧内容的模型。' : '')
      || (!parseReady ? '请展开解析设置，为多模态解析选择支持图片的模型。' : '')
      || '材料、比较要求与模型已就绪，可以开始。'
  const error = media.error || connections.error || profiles.error || listing.error || detail.error || saveProfile.error || suggest.error || create.error
  return <div className="comparison-workbench">
    {detail.data && <ComparisonResults key={detail.data.comparison_id} workspace={workspace} run={detail.data} connections={available} refresh={refresh}
      latestRevision={Math.max(detail.data.revision, latest.data?.revision || 0, listing.data?.items.find(item => item.comparison_id === selected)?.revision || 0)}
      selectRevision={value => setParams(previous => { const next = new URLSearchParams(previous); const latestRevision = latest.data?.revision || listing.data?.items.find(item => item.comparison_id === selected)?.revision || detail.data?.revision;
        if (value === latestRevision) next.delete('comparison_revision'); else next.set('comparison_revision', String(value)); return next })} />}
    <section className="panel" aria-label="比较材料与模型" inert={!!pending}><div className="section-head"><div><Title level={3}>1. 准备两侧材料</Title><Text type="secondary">A 侧：扫描 PDF · B 侧：PPTX 或有序幻灯片图片</Text></div>
      <Space wrap><Link to={navigationHref('/assets', params.toString(), '', '/apps/comparisons')}>资料库</Link><Button onClick={() => setShowConnections(true)}>管理模型连接</Button></Space></div>
      <Paragraph type="secondary">选择文件后会自动上传，再分别指定 A、B 两侧材料。也可以直接选择项目中已有的文件。</Paragraph>
      <UploadQueue workspaceId={workspace} compact autoUpload accept=".pdf,.pptx,.png,.jpg,.jpeg,.webp,.bmp,.tif,.tiff" buttonLabel="选择比较文件" disabled={create.isPending} onBusy={setUploadBusy} onUploaded={() => void media.refetch()} />
      <div className="comparison-material-columns"><section aria-label="A 侧材料"><Title level={4}>A / PDF 文档</Title><MaterialSelector label="A 侧 PDF" items={mediaList.filter(item => /\.pdf$/i.test(item.original_name))} selected={left} change={setLeft} disabled={uploadBusy || create.isPending} />
        <details className="comparison-parse-settings"><summary>A 侧解析设置 · {leftLayout.mode === 'local_ocr' ? '本地 OCR' : '多模态 API'}</summary><LayoutOptions label="A 侧" value={leftLayout} onChange={setLeftLayout} connections={available} /></details></section>
        <section aria-label="B 侧材料"><Title level={4}>B / 幻灯片</Title><MaterialSelector label="B 侧 PPTX 或图片" items={mediaList.filter(item => /\.(pptx|png|jpe?g|webp|bmp|tiff?)$/i.test(item.original_name))} selected={right} change={setRight} multiple disabled={uploadBusy || create.isPending} />
          <details className="comparison-parse-settings"><summary>B 侧解析设置 · {rightLayout.mode === 'local_ocr' ? '本地 OCR' : '多模态 API'}</summary><LayoutOptions label="B 侧" value={rightLayout} onChange={setRightLayout} connections={available} /></details></section></div>
      {!!left.length && !!right.length && materialError && <Alert type="warning" title={materialError} />}
      <Paragraph type="secondary">PDF 最多 200 页；PPTX / 图片最多 100 页；单文件 128 MiB。PPTX 使用静态页面，包含隐藏页并标记，备注不参与。</Paragraph>
    </section>
    <section className="panel" aria-label="比较主题配置" inert={!!pending}><div className="section-head"><div><Title level={3}>2. 定义比较主题</Title><Text type="secondary">按指定字段提取事实，对照原文解释差异</Text></div>
      <Select aria-label="选择比较配置" style={{ minWidth: 230 }} placeholder="已保存的比较配置" value={savedProfile?.id} options={profiles.data?.items.map(item => ({ value: item.id, label: `${item.name} · v${item.version}` }))}
        onChange={id => { const selectedProfile = profiles.data?.items.find(item => item.id === id); if (selectedProfile) { setProfile(selectedProfile); setSavedProfile(selectedProfile); setAliasesValid(true); setNotice('') } }} /></div>
      <Form layout="vertical"><ProviderSelect label="比较模型（独立于逐页解析模型）" value={providerId} connections={available} onChange={item => setProviderId(item.id)} /></Form>
      <div className="comparison-topic-summary"><Text strong>{profile.name}</Text><Paragraph>{profile.instructions}</Paragraph><Text type="secondary">比较字段：{profile.fields.slice(0, 6).map(field => field.label).join('、')}{profile.fields.length > 6 ? `等 ${profile.fields.length} 项` : ''}</Text></div>
      <details className="comparison-profile-editor" open={profileDirty}><summary>调整比较要求与字段{profileDirty ? ' · 尚未保存' : ''}</summary><ProfileEditor key={profile.id} value={profile} onChange={setProfile} onValidityChange={setAliasesValid} />
      <Space wrap className="comparison-profile-actions"><Button loading={suggest.isPending} disabled={!provider || !profile.instructions.trim()} onClick={() => suggest.mutate()}>根据要求生成字段建议</Button>
        {savedProfile && <Tag>{profileDirty ? '存在未保存修改' : `已保存 v${savedProfile.version}`}</Tag>}
        <Button onClick={() => { setProfile({ ...profile, id: `profile_${crypto.randomUUID().replaceAll('-', '')}`, version: 1, name: `${profile.name}（副本）` }); setSavedProfile(null); setAliasesValid(true) }}>另存为新配置</Button></Space></details>
      {fieldsError && <Alert type="warning" title={fieldsError} />}
      <Paragraph type="secondary">比较模型接收两侧解析文字与表格。日期口径不同或解析不完整时，结果会注明信息不足。</Paragraph>
    </section>
    <section className="panel comparison-submit" aria-label="启动比较"><Title level={3}>3. 生成统一报告</Title>
      <ul className="comparison-ready-list" aria-label="比较准备情况"><li><Tag color={!materialError ? 'success' : 'default'}>{!materialError ? '已准备' : '待选择'}</Tag>A 侧 PDF 与 B 侧幻灯片</li><li><Tag color={!fieldsError && aliasesValid && !profileDirty ? 'success' : 'default'}>{!fieldsError && aliasesValid && !profileDirty ? '已保存' : '待保存'}</Tag>比较要求与字段</li><li><Tag color={provider && parseReady ? 'success' : 'default'}>{provider && parseReady ? '已选择' : '待选择'}</Tag>解析与比较模型</li></ul>
      <details className="comparison-task-name"><summary>修改任务名称 · {title}</summary><Form layout="vertical"><Form.Item label="任务名称"><Input aria-label="任务名称" value={title} onChange={event => setTitle(event.target.value)} /></Form.Item></Form></details>
      {notice && <Alert type="success" title={notice} />}{error && <Alert type="error" title={error.message} />}
      {pending && <Alert type="warning" title="上次提交结果待核实" description="材料和配置已锁定；继续将沿用原请求编号、原材料和配置版本，避免重复创建任务。" />}
      <Paragraph className="comparison-next-step" role="status">{nextStep}</Paragraph>
      <Space wrap>{!pending && profileDirty && <Button loading={saveProfile.isPending} disabled={!!fieldsError || !aliasesValid || create.isPending} onClick={() => saveProfile.mutate()}>保存配置版本</Button>}<Button type="primary" loading={create.isPending || uploadBusy} disabled={uploadBusy || (!pending && (!!materialError || !!fieldsError || !aliasesValid || profileDirty || !provider || !parseReady))} onClick={() => create.mutate()}>{uploadBusy ? '正在准备材料…' : pending ? '核实原请求并继续' : '开始解析与比较'}</Button>
        <Button onClick={() => { refresh(); void media.refetch(); void connections.refetch() }}>刷新状态</Button></Space>
      <Paragraph type="secondary">HTML 报告采用固定模板、字体和章节；各项结论附来源证据。任务创建后锁定材料与配置版本。</Paragraph>
    </section>
    {!!listing.data?.items.length && <section className="panel" aria-label="对比任务历史"><Title level={3}>任务与历史版本</Title><Table<ComparisonRun> rowKey="comparison_id" size="small" dataSource={listing.data.items} pagination={{ current: historyPage, pageSize: 5, total: listing.data.total, onChange: setHistoryPage, showSizeChanger: false }} columns={[
      { title: '任务', dataIndex: 'title' }, { title: '状态', render: (_, run) => <Tag>{stateNames[run.status] || run.status}</Tag> },
      { title: '修订', render: (_, run) => `v${run.revision}` }, { title: '操作', render: (_, run) => <Button type="link" onClick={() => choose(run)}>查看结果</Button> },
    ]} /></section>}
    {showConnections && <ConnectionSettings workspace={workspace} connections={available} close={() => setShowConnections(false)} saved={() => void connections.refetch()} />}
  </div>
}

export function ComparisonWorkbench() {
  const workspace = useWorkspaceId()
  return <><div className="page-heading"><Text className="eyebrow">CLEAR / DOCUMENT COMPARISON</Text><Title level={1}>文档主题对比</Title>
    <Paragraph>解析两侧材料，围绕指定信息比较差异，生成可追溯的统一报告。</Paragraph></div>
    {workspace ? <ComparisonPanel key={workspace} workspace={workspace} /> : <Workspaces continueTo="/apps/comparisons" />}</>
}
