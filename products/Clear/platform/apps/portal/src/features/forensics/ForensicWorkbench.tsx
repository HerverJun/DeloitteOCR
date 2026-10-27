import { ToolIdentity } from '../catalog/ToolIdentity'
import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Checkbox, Form, Input, Pagination, Select, Space, Table, Tag, Typography } from 'antd'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import type { Asset, ForensicEvidenceSelection, Inspection, InspectionCreate, InspectionPage, InspectionReview } from '../../../../../packages/contracts/types.generated'
import { useWorkspaceId, Workspaces } from '../../App'
import { brands, applicationDescriptions } from '../../clear-ui/theme'
import { navigationHref } from '../../clear-ui/navigation'
import { ApplicationDeclaration, forensicsDeclaration } from '../catalog/ApplicationDeclaration'
import { API, localReviewer, newRequestId, post, request } from '../batches/api'
import { UploadQueue } from '../batches/UploadQueue'
import { ForensicImageViewer } from './ForensicImageViewer'
import { imageSize, readGeometry } from './geometry'
import './forensics.css'

const { Title, Text, Paragraph } = Typography
const tools = [{ value: 'metadata', label: '文件画像' }, { value: 'noise', label: '噪声残差' }, { value: 'frequency', label: '高频分布' },
  { value: 'jpeg', label: 'JPEG 重压缩差异' }, { value: 'jpeg_grid', label: 'JPEG 网格' }, { value: 'copy_move', label: '重复区域' }] as const
const names: Record<string, string> = Object.fromEntries(tools.map(tool => [tool.value, tool.label]))
const statuses: Record<string, string> = { queued: '排队', running: '取证中', succeeded: '报告已生成', partial: '部分工具或归集失败', failed: '执行失败', needs_verification: '执行中断待核实', completed: '已执行', skipped: '不适用 · 已跳过', error: '工具失败' }
const assessments = [{ value: 'needs_investigation', label: '需要进一步调查' }, { value: 'inconclusive', label: '无法判断' }, { value: 'no_supported_clue', label: '当前工具未提供可支持的线索' }]
type ToolEvidence = { tool: string; status: string; version: string; elapsed_ms: number; map_path: string | null; reason: string | null; limitations: string[]; metrics: Record<string, unknown> }
const path = (workspace: string) => `/workspaces/${encodeURIComponent(workspace)}/forensics`
function toolHref(target: string, workspaceId: string | null, location: { search: string; pathname: string }, extra: Record<string, string> = {}) {
  const scoped = new URLSearchParams(navigationHref(target, location.search, '', location.pathname).split('?')[1])
  const params = new URLSearchParams()
  const workspace = workspaceId || scoped.get('workspace')
  if (workspace) params.set('workspace', workspace)
  if (scoped.get('task_return')) params.set('task_return', scoped.get('task_return')!)
  for (const [key, value] of Object.entries(extra)) params.set(key, value)
  return `${target}${params.size ? `?${params}` : ''}`
}

export function ForensicApplicationCard() {
  const workspaceId = useWorkspaceId()
  const location = useLocation()
  const linked = toolHref('/apps/forensics', workspaceId, location)
  return <Card className="application-card" title={<ToolIdentity appId="forensics" to={linked} label={brands.forensics} />}>
    <Text className="application-purpose">{applicationDescriptions.forensics.purpose}</Text>
    <Paragraph className="application-description" type="secondary">{applicationDescriptions.forensics.description}</Paragraph>
    <div className="application-state"><Tag>经典取证工具</Tag><Text type="secondary">{workspaceId ? '使用当前项目' : '选择或新建项目后开始'}</Text></div>
    <Link className="application-actions" to={linked}><Button type="primary">打开图像取证</Button></Link>
    <ApplicationDeclaration declaration={forensicsDeclaration} />
  </Card>
}

export function ForensicEvidenceView({ inspection }: { inspection: Inspection }) {
  const location = useLocation()
  const records = (inspection.report?.evidence || []) as ToolEvidence[]
  const [active, setActive] = useState('')
  const input = (inspection.report?.input || {}) as Record<string, unknown>
  const geometry = readGeometry(input.geometry)
  const size = geometry?.display_size || imageSize([input.width, input.height]) || [1000, 1000]
  const selected = records.find(tool => tool.tool === active) || records.find(tool => tool.map_path)
  const compatibleMap = !!geometry && selected?.metrics.map_coordinate_system === 'exif_transposed_pixels'
    && selected.metrics.coordinate_version === geometry.coordinate_version
  const preview = geometry && typeof input.preview_path === 'string' ? input.preview_path : 'preview-exif-normalized.png'
  const coordinateWarning = !geometry ? '旧版报告缺少可核验的坐标元数据，保留原图预览；热图仅独立查看，不能确认图层配准。'
    : selected?.map_path && !compatibleMap ? '此热图缺少匹配的坐标版本，已停用叠加；请独立查看热图。' : undefined
  const file = (name: string) => `${API}${path(inspection.workspace_id)}/${encodeURIComponent(inspection.inspection_id)}/files/${encodeURIComponent(name)}`
  return <section className="forensic-evidence">
    <Paragraph>原件：<Link to={toolHref('/assets', inspection.workspace_id, location, { asset: inspection.asset_id })}>{inspection.original_name}</Link> · 来源版本 {inspection.asset_revision} · 审阅版本 {inspection.revision}</Paragraph>
    <Paragraph type="secondary">热图表示该工具的相对统计量。转发压缩、文字边缘和重复纹理也会产生变化；工具跳过或失败不能解释为未篡改。</Paragraph>
    {inspection.report && <><Space wrap><Text>选择检查图层，查看原图细节或叠加对照</Text>
      <Select aria-label="查看取证热图" value={selected?.tool} onChange={setActive} options={records.filter(tool => tool.map_path).map(tool => ({ value: tool.tool, label: names[tool.tool] || tool.tool }))} /></Space>
      <ForensicImageViewer image={file(preview)} heatmap={selected?.map_path ? file(selected.map_path) : undefined}
        width={size[0]} height={size[1]} registered={compatibleMap} warning={coordinateWarning}
        heatmapLabel={selected ? `${names[selected.tool] || selected.tool} · 灰度热图` : undefined} />
      <Table<ToolEvidence> size="small" rowKey="tool" pagination={false} dataSource={records} columns={[
        { title: '工具', dataIndex: 'tool', render: name => names[name] || name },
        { title: '执行情况', dataIndex: 'status', render: value => <Tag color={value === 'error' ? 'error' : undefined}>{statuses[value] || value}</Tag> },
        { title: '适用与限制', render: (_, tool) => <>{tool.reason && <Paragraph>{tool.reason}</Paragraph>}<ul>{tool.limitations.map((value, index) => <li key={index}>{value}</li>)}</ul></> },
        { title: '版本 / 耗时', render: (_, tool) => `${tool.version} · ${tool.elapsed_ms} ms` },
      ]} />
      <details className="application-message"><summary>查看原始指标和来源身份</summary><pre>{JSON.stringify({ input: inspection.report.input, report_hash: inspection.report_hash, tools: records.map(({ tool, metrics }) => ({ tool, metrics })) }, null, 2)}</pre></details>
      <Space wrap className="application-message"><a href={`${API}${path(inspection.workspace_id)}/${inspection.inspection_id}/download`}>下载完整报告（含图片）</a>
        {inspection.files.filter(value => value.name === 'report.json').map(value => <a key={value.name} href={file(value.name) + '?download=true'}>下载 {value.name}</a>)}</Space>
    </>}
    {inspection.review && <Paragraph><Text strong>固定人工审阅：</Text>{assessments.find(value => value.value === inspection.review?.assessment)?.label} · {String(inspection.review.note)} · {String((inspection.review.actor as { display_name: string }).display_name)}</Paragraph>}
  </section>
}

export function InspectionTasks({ workspaceId }: { workspaceId: string }) {
  const location = useLocation()
  const [page, setPage] = useState(1)
  const list = useQuery({ queryKey: ['forensics', workspaceId, 'list', page],
    queryFn: ({ signal }) => request<InspectionPage>(`${path(workspaceId)}?offset=${(page - 1) * 20}&limit=20`, { signal }),
    refetchInterval: query => query.state.data?.items.some(item => ['queued', 'running'].includes(item.status)) ? 2000 : false })
  return <section className="panel"><div className="section-head"><Title level={3}>图像取证任务</Title><Button onClick={() => void list.refetch()}>刷新取证任务</Button></div>
    {list.error && <Alert type="error" title={list.error.message} />}
    <Table<Inspection> loading={list.isPending} rowKey="inspection_id" dataSource={list.data?.items || []} pagination={false} size="small" columns={[
      { title: '图片', dataIndex: 'original_name' }, { title: '状态', dataIndex: 'status', render: value => <Tag>{statuses[value]}</Tag> },
      { title: '人工审阅', render: (_, value) => value.review ? '已登记说明' : '待审阅' },
      { title: '操作', render: (_, value) => <Link to={toolHref('/apps/forensics', workspaceId, location, { inspection: value.inspection_id })}>查看报告与审阅</Link> },
    ]} />
    <Pagination current={page} pageSize={20} total={list.data?.total || 0} onChange={setPage} showSizeChanger={false} />
  </section>
}

export function ForensicPicker({ workspaceId, disabled, selected, onSelected }: { workspaceId: string; disabled: boolean; selected: ForensicEvidenceSelection[]; onSelected: (items: ForensicEvidenceSelection[]) => void }) {
  const location = useLocation()
  const [page, setPage] = useState(1)
  const list = useQuery({ queryKey: ['forensics', workspaceId, 'picker', page], queryFn: ({ signal }) => request<InspectionPage>(`${path(workspaceId)}?offset=${(page - 1) * 20}&limit=20`, { signal }), refetchOnMount: 'always' })
  return <section className="panel"><div className="section-head"><Title level={3}>选择图像取证证据</Title><Link to={toolHref('/apps/forensics', workspaceId, location)}>打开 {brands.forensics}</Link></div>
    <Paragraph>选择已生成报告且已填写人工审阅说明的图片。保存调查时固定报告和审阅版本。</Paragraph>
    {list.error && <Alert type="error" title={list.error.message} action={<Button onClick={() => void list.refetch()}>刷新</Button>} />}
    <Table<Inspection> rowKey="inspection_id" loading={list.isPending} dataSource={list.data?.items || []} pagination={false} size="small" rowSelection={{
      selectedRowKeys: selected.map(item => item.inspection_id), getCheckboxProps: item => ({ disabled: disabled || !item.review || !item.report_hash || !['succeeded', 'partial'].includes(item.status) }),
      onChange: keys => { const ids = new Set(list.data?.items.map(item => item.inspection_id)); onSelected([...selected.filter(item => !ids.has(item.inspection_id)),
        ...(list.data?.items || []).filter(item => keys.includes(item.inspection_id) && item.review && item.report_hash).map(item => ({ schema_version: '1.0' as const, inspection_id: item.inspection_id, inspection_revision: item.revision, report_hash: item.report_hash! }))]) },
    }} columns={[{ title: '图片', dataIndex: 'original_name' }, { title: '状态', dataIndex: 'status', render: value => statuses[value] },
      { title: '人工审阅版本', dataIndex: 'revision' }, { title: '详情', render: (_, item) => <Link to={toolHref('/apps/forensics', workspaceId, location, { inspection: item.inspection_id })}>查看取证</Link> }]} />
    <Pagination current={page} pageSize={20} total={list.data?.total || 0} onChange={setPage} showSizeChanger={false} />
  </section>
}

function Review({ inspection }: { inspection: Inspection }) {
  const location = useLocation()
  const client = useQueryClient(), [form] = Form.useForm(), [pending, setPending] = useState<InspectionReview | null>(null)
  const key = `clear-forensic-review:${inspection.workspace_id}:${inspection.inspection_id}`
  useEffect(() => { try { setPending(JSON.parse(localStorage.getItem(key) || 'null')) } catch { setPending(null) }
    form.setFieldsValue({ assessment: inspection.review?.assessment || 'inconclusive', note: inspection.review?.note || '', actor: localReviewer().display_name })
  }, [inspection.inspection_id, inspection.revision, key, form])
  const save = useMutation({ mutationFn: (payload: InspectionReview) => post<Inspection>(`${path(inspection.workspace_id)}/${inspection.inspection_id}/review`, payload), onSuccess: () => {
    localStorage.removeItem(key); setPending(null); void client.invalidateQueries({ queryKey: ['forensics', inspection.workspace_id] }); void client.invalidateQueries({ queryKey: ['todos', inspection.workspace_id] })
  } })
  function submit(values: { actor: string; assessment: InspectionReview['assessment']; note: string }) {
    const actor = { ...localReviewer(), display_name: values.actor.trim() }
    localStorage.setItem('clear-local-reviewer', JSON.stringify(actor))
    const payload: InspectionReview = { schema_version: '1.0', request_id: newRequestId(), expected_revision: inspection.revision, actor, assessment: values.assessment, note: values.note }
    localStorage.setItem(key, JSON.stringify(payload)); setPending(payload); save.mutate(payload)
  }
  return <section className="panel"><Title level={3}>人工审阅与调查判断</Title>
    <Form form={form} layout="vertical" onFinish={submit} disabled={save.isPending || !!pending}>
      <Form.Item name="actor" label="本机审阅人" rules={[{ required: true, whitespace: true }]}><Input maxLength={200} /></Form.Item>
      <Form.Item name="assessment" label="当前证据支持的判断"><Select options={assessments} /></Form.Item>
      <Form.Item name="note" label="审阅说明（可疑位置、依据与限制）" rules={[{ required: true, whitespace: true }]}><Input.TextArea rows={4} maxLength={4000} /></Form.Item>
      <Button type="primary" htmlType="submit">保存人工审阅</Button>
    </Form>
    {save.error && <Alert className="application-message" type="error" title={save.error.message} />}
    {pending && <Space wrap className="application-message"><Text>已保留原请求。响应丢失可核对重试；版本冲突后刷新再修改。</Text><Button loading={save.isPending} onClick={() => save.mutate(pending)}>核对原请求</Button>
      <Button disabled={save.isPending} onClick={() => { localStorage.removeItem(key); setPending(null); void client.invalidateQueries({ queryKey: ['forensics', inspection.workspace_id] }) }}>放弃此未确认草稿并刷新</Button></Space>}
    <Link to={toolHref('/audit', inspection.workspace_id, location)}>进入审计项目，将此证据关联调查事项</Link>
  </section>
}

function Workspace({ workspaceId }: { workspaceId: string }) {
  const location = useLocation()
  const [params, setParams] = useSearchParams(), client = useQueryClient(), selected = params.get('inspection') || ''
  const [assetId, setAssetId] = useState(() => params.get('asset') || ''), [chosen, setChosen] = useState<InspectionCreate['tools']>(tools.map(tool => tool.value))
  const [assetPage, setAssetPage] = useState(1), [keyword, setKeyword] = useState('')
  const [uploadBusy, setUploadBusy] = useState(false)
  const pendingKey = `clear-forensic-start:${workspaceId}`
  const [pending, setPending] = useState<InspectionCreate | null>(() => { try { return JSON.parse(localStorage.getItem(pendingKey) || 'null') } catch { return null } })
  const assets = useQuery({ queryKey: ['assets', workspaceId, 'forensics', assetPage, keyword], queryFn: ({ signal }) => request<{ items: Asset[]; total: number }>(`/workspaces/${encodeURIComponent(workspaceId)}/assets?offset=${(assetPage - 1) * 50}&limit=50&media_type=image%2F*&keyword=${encodeURIComponent(keyword)}`, { signal }) })
  const listedAsset = assets.data?.items.find(item => item.asset_id === assetId)
  const selectedAsset = useQuery({ queryKey: ['assets', workspaceId, 'forensic-selected', assetId], enabled: !!assetId && !listedAsset,
    queryFn: ({ signal }) => request<Asset>(`/workspaces/${encodeURIComponent(workspaceId)}/assets/${encodeURIComponent(assetId)}`, { signal }) })
  const asset = listedAsset || (selectedAsset.data?.asset_id === assetId ? selectedAsset.data : undefined)
  const imageReady = !!asset && asset.media_type.startsWith('image/')
  const detail = useQuery({ queryKey: ['forensics', workspaceId, selected], enabled: !!selected, queryFn: ({ signal }) => request<Inspection>(`${path(workspaceId)}/${selected}`, { signal }), refetchOnMount: 'always',
    refetchInterval: query => ['queued', 'running'].includes(query.state.data?.status || '') ? 1500 : false })
  const start = useMutation({ mutationFn: (payload: InspectionCreate) => post<Inspection>(path(workspaceId), payload), onSuccess: item => {
    localStorage.removeItem(pendingKey); setPending(null); setParams(previous => { const next = new URLSearchParams(previous); next.set('workspace', workspaceId); next.set('inspection', item.inspection_id); return next }); void client.invalidateQueries({ queryKey: ['forensics', workspaceId] })
  } })
  const action = useMutation({ mutationFn: (name: string) => post(`${path(workspaceId)}/${selected}/${name}`, {}), onSuccess: () => {
    void client.invalidateQueries({ queryKey: ['forensics', workspaceId] }); void client.invalidateQueries({ queryKey: ['artifact-bundles', workspaceId] }) } })
  function submit() { if (!asset || !imageReady || uploadBusy || pending || !chosen.length) return
    const payload: InspectionCreate = { schema_version: '1.0', request_id: newRequestId(), asset_id: asset.asset_id, asset_revision: asset.revision, blob_hash: asset.blob_hash, tools: chosen }
    localStorage.setItem(pendingKey, JSON.stringify(payload)); setPending(payload); start.mutate(payload) }
  const startHint = pending ? '原提交尚未确认，请查询同一请求，避免重复检查。' : uploadBusy ? '正在准备图片，完成后即可开始。'
    : !assetId ? '选择图片后自动上传，上传完成即可开始。' : !imageReady ? selectedAsset.isFetching || assets.isFetching ? '正在确认所选图片…' : '所选图片暂不可用，请重新选择。'
    : !chosen.length ? '请在处理设置中至少选择一项检查。' : `已选 ${asset?.original_name} · ${chosen.length} 项检查已就绪。`
  const imageOptions = (assets.data?.items || []).filter(item => item.media_type.startsWith('image/'))
  if (imageReady && asset && !imageOptions.some(item => item.asset_id === asset.asset_id)) imageOptions.unshift(asset)
  return <><details className="panel forensic-start" open={!detail.data?.report || !!pending}>
    <summary className="forensic-start-title"><span>选择图片与取证工具</span><small>{detail.data?.report ? '展开，开始另一项检查' : '选择图片 → 开始检查'}</small></summary>
    <div className="forensic-start-body"><Paragraph>选择图片后自动上传并选中，再点击开始。默认执行全部六项检查；不适用的检查会自动跳过并说明原因。</Paragraph>
    <UploadQueue workspaceId={workspaceId} compact autoUpload accept=".jpg,.jpeg,.png,.webp,.bmp,.tif,.tiff" multiple={false} buttonLabel="选择图片"
      disabled={!!pending || start.isPending} onBusy={setUploadBusy} onFilesSelected={() => setAssetId('')}
      onUploaded={id => { setAssetId(id); setKeyword(''); setAssetPage(1); void assets.refetch() }} />
    <div className="application-message forensic-asset-actions"><Select className="forensic-asset-select" aria-label="取证图片" placeholder="或选择项目中已有的图片" value={assetId || undefined} onChange={setAssetId} loading={assets.isPending} disabled={!!pending || uploadBusy}
      options={imageOptions.map(item => ({ value: item.asset_id, label: item.original_name }))} /><Button onClick={() => { void assets.refetch(); if (assetId) void selectedAsset.refetch() }}>刷新图片</Button></div>
    {assets.error && <Alert type="error" title={assets.error.message} />}
    {assetId && selectedAsset.error && !listedAsset && <Alert type="error" title={selectedAsset.error.message} />}
    <div className="forensic-start-actions"><Button type="primary" size="large" onClick={() => pending ? start.mutate(pending) : submit()} disabled={uploadBusy || !pending && (!imageReady || !chosen.length)} loading={start.isPending || uploadBusy}>{uploadBusy ? '正在准备图片…' : pending ? '查询并续接原提交' : '开始本地取证'}</Button><Text type="secondary" role="status">{startHint}</Text></div>
    <details className="application-message forensic-find"><summary>查找其他图片</summary>
      <Input.Search aria-label="搜索取证图片" placeholder="按图片名称搜索" disabled={!!pending || uploadBusy} onSearch={value => { setKeyword(value); setAssetPage(1); setAssetId('') }} />
      {!!assets.data?.total && <Pagination current={assetPage} pageSize={50} total={assets.data.total} onChange={value => { setAssetPage(value); setAssetId('') }} showSizeChanger={false} disabled={!!pending || uploadBusy} />}
      <Link to={toolHref('/assets', workspaceId, location)}>打开项目资料库</Link>
    </details>
    <details className="application-message"><summary>处理设置 · 已选择 {chosen.length} 项检查</summary><Checkbox.Group options={[...tools]} value={chosen} disabled={!!pending} onChange={values => setChosen(values as InspectionCreate['tools'])} /></details>
    <Paragraph>支持单帧 JPEG、PNG、WebP、BMP、TIFF，最大 32 MiB、1200 万像素。</Paragraph>
    {start.error && <Alert type="error" title={start.error.message} />}
    {pending && <Space wrap><Text>原提交已保留，刷新后可核对同一任务。</Text><Button disabled={start.isPending} onClick={() => { localStorage.removeItem(pendingKey); setPending(null) }}>结束此提交草稿</Button></Space>}
    </div></details>
    {detail.error && <Alert type="error" title={detail.error.message} action={<Button onClick={() => void detail.refetch()}>刷新</Button>} />}
    {detail.data && <><section className="panel forensic-report"><div className="section-head"><Title level={3}>{detail.data.original_name}</Title><Tag>{statuses[detail.data.status]}</Tag></div>
      {detail.data.error && <Alert type="warning" title={detail.data.error} />}
      <ForensicEvidenceView inspection={detail.data} />
      <Space wrap>{detail.data.status === 'needs_verification' && <Button loading={action.isPending} onClick={() => action.mutate('resume')}>恢复此中断任务</Button>}
        {detail.data.report && <><Button loading={action.isPending} onClick={() => action.mutate('collect')}>归集固定报告到成果中心</Button><Link to={toolHref('/artifacts', workspaceId, location, { bundle: selected })}>查看取证成果</Link></>}</Space>
      {action.error && <Alert type="error" title={action.error.message} />}
    </section>{detail.data.report && <details className="panel"><summary>可选：添加人工审阅说明</summary><Review inspection={detail.data} /></details>}</>}
    <InspectionTasks workspaceId={workspaceId} /></>
}

export function ForensicWorkbench() {
  const workspaceId = useWorkspaceId(), location = useLocation()
  return <div className="forensic-workbench"><div className="page-heading"><div className="eyebrow">APPLICATION / IMAGE FORENSICS</div><Title level={1}>{brands.forensics}</Title><Paragraph>选择或上传图片，开始检查，直接查看和下载报告。</Paragraph></div>
    <Paragraph><Link to={navigationHref('/apps/forensics/deep', location.search, location.hash, location.pathname)}>打开 SAFIRE 深度定位 →</Link> · <Link to={navigationHref('/apps/forensics/document', location.search, location.hash, location.pathname)}>文档篡改排查 →</Link> · <Link to={navigationHref('/apps/forensics/aigc', location.search, location.hash, location.pathname)}>整图 AI 生成检测 →</Link></Paragraph>
    {workspaceId ? <Workspace key={workspaceId} workspaceId={workspaceId} /> : <Workspaces continueTo="/apps/forensics" />}
    <div className="back-link"><Link to={navigationHref('/apps', location.search, location.hash, location.pathname)}>← 返回应用目录</Link></div></div>
}
