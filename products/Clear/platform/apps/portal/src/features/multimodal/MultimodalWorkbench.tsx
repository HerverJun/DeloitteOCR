import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Card, Empty, Image, Input, InputNumber, Select, Skeleton, Space, Table, Tag, Typography } from 'antd'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useWorkspaceId, Workspaces } from '../../App'
import { API, assets, newRequestId, post, request, stateNames, type Asset } from '../batches/api'
import { UploadQueue } from '../batches/UploadQueue'
import { RegionReview, type Mark } from './RegionReview'
import { readGeometry } from '../forensics/geometry'
import { ToolIdentity } from '../catalog/ToolIdentity'
import './multimodal.css'

const { Title, Paragraph, Text } = Typography
type Kind = 'face' | 'audio' | 'seal' | 'catnet'
type Capability = { kind: Kind; title: string; state: string; cpu_ready: boolean; gpu_ready: boolean; missing: string[] }
type Face = { face_index: number; box: number[]; detection_score: number; crop: string; quality: { usable: boolean; note: string } }
type Segment = { row_id: string; review_index: number; start_ms: number; end_ms: number; text: string; speaker: string }
type Seal = { row_id: string; review_index: number; page_number: number; box?: number[]; text: string; subject: string; number: string; subject_check: string }
type Result = { outcome: string; algorithm: string; device: string; elapsed_seconds: number; limitations: string[];
  processing?: { geometry?: unknown; display_preview?: string; display_heatmap?: string }; output_files?: Record<string, { bytes: number; sha256: string }>;
  width?: number; height?: number; pages?: { page_number: number; width: number; height: number; preview: string }[];
  images?: { faces: Face[]; width: number; height: number; preview: string }[]; comparison?: { cosine_similarity: number; meets_reference_threshold: boolean } | null;
  candidates?: { reference_id: string; label: string; score: number; meets_reference_threshold: boolean }[];
  segments?: Segment[]; seals?: Seal[]; audio?: { duration_ms: number; original_channels: number; original_sample_rate: number } }
type Edit = { row_id?: string; index: number; text?: string; speaker?: string; subject?: string; number?: string }
type Analysis = { job_id: string; kind: Kind; status: string; stage: string; revision: number; error: string | null; result_hash: string | null;
  progress?: { processed_ms?: number; duration_ms?: number; completed_chunks?: number; total_chunks?: number }; actual_device?: string | null;
  inputs: { original_name: string; asset_id: string; asset_revision: number }[]; result: Result | null; created_at: string;
  review: { actor: string; note: string; assessment: string; edits: Edit[]; regions?: Mark[] } | null }
type Reference = { reference_id: string; label: string; job_id: string; source: { original_name: string } }
const names: Record<Kind, string> = { face: '人脸比对', audio: '音频分析', seal: '印章识别', catnet: '深度图像取证' }
const descriptions: Record<Kind, string> = {
  face: '检测照片中的人脸，进行两图比对或检索当前项目参考库，保存人工核对结果。',
  audio: '回听录音、查看波形与频谱，校正带时间定位和匿名说话人的中文转写。',
  seal: '定位文档中的多个印章，校正章内文字与编号，再核对主体名称。',
  catnet: '使用 CAT-Net 查看 JPEG 可疑区域，并结合经典工具和原件进行人工排查。',
}
const routes = { face: '/apps/face', audio: '/apps/audio', seal: '/apps/ocr/seals', catnet: '/apps/forensics/deep' } as const
const outcomes: Record<string, string> = { detected: '已检出人脸', no_face: '未检出可用人脸', compared: '比对已完成', quality_insufficient: '图片质量不足',
  candidates: '已找到参考候选', no_trusted_candidate: '未找到达到参考阈值的候选', transcribed: '转写已完成', no_speech: '未识别出语音', recognized: '已完成印章识别', no_seal: '未检出印章', analyzed: '分析已完成' }
const clock = (ms: number) => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`
const base = (workspace: string) => `/workspaces/${encodeURIComponent(workspace)}/multimodal`

export function MultimodalApplicationCard({ kind }: { kind: 'face' | 'audio' }) {
  const [params] = useSearchParams()
  const capability = useQuery({ queryKey: ['multimodal-capabilities'], queryFn: () => request<{ items: Capability[] }>('/multimodal/capabilities') })
  const current = capability.data?.items.find(value => value.kind === kind)
  const to = routes[kind] + (params.size ? `?${params}` : '')
  return <Card className="application-card" title={<ToolIdentity appId={kind} to={to} label={names[kind]} />}>
    <Text className="application-purpose">{kind === 'face' ? '静态照片核对与项目参考库' : '中文录音转写与片段复核'}</Text>
    <Paragraph className="application-description" type="secondary">{descriptions[kind]}</Paragraph>
    <div className="application-state"><Tag color={current?.cpu_ready ? 'success' : current?.missing.length ? 'warning' : 'default'}>
      {capability.isPending ? '正在核实' : current?.cpu_ready ? 'CPU 推理已验证' : current?.missing.length ? '资源待准备' : '待推理验证'}</Tag>
      <Text type="secondary">{kind === 'face' ? '项目内独立参考库' : '原音时间定位与人工修订'}</Text></div>
    {capability.isError && <Alert type="error" title="能力状态暂不可读取" />}
    <Link className="application-actions" to={to}><Button type="primary">打开{names[kind]}</Button></Link>
    <details className="application-details"><summary>查看工具声明</summary><Paragraph>{kind === 'face' ? 'YuNet / SFace。相似度与候选名称均需人工核对；静态照片分析不提供活体结论。' : 'Paraformer / FSMN-VAD / ct-punc / CAM++。匿名说话人由人工命名，引用片段前需回听原音。'}</Paragraph></details>
  </Card>
}

function Workspace({ kind, workspaceId }: { kind: Kind; workspaceId: string }) {
  const client = useQueryClient(), [params, setParams] = useSearchParams()
  const [selectedInputs, setSelectedInputs] = useState<string[]>([]), [action, setAction] = useState('detect')
  const [faceIndices, setFaceIndices] = useState([0, 0]), [subject, setSubject] = useState('')
  const [device, setDevice] = useState('cpu'), [referenceName, setReferenceName] = useState('')
  const [channel, setChannel] = useState<number | null>(null)
  const [page, setPage] = useState(1)
  const selected = params.get('analysis') || ''
  const list = useQuery({ queryKey: ['manual-analysis', workspaceId, kind, page], queryFn: () => request<{ items: Analysis[]; total: number }>(`${base(workspaceId)}/jobs?kind=${kind}&offset=${(page - 1) * 10}&limit=10`),
    refetchInterval: query => query.state.data?.items.some(item => ['queued', 'running', 'cancelling'].includes(item.status)) ? 1500 : false })
  const media = useQuery({ queryKey: ['multimodal-assets', workspaceId], queryFn: ({ signal }) => assets(workspaceId, signal) })
  const capabilities = useQuery({ queryKey: ['multimodal-capabilities'], queryFn: () => request<{ items: Capability[] }>('/multimodal/capabilities') })
  const capability = capabilities.data?.items.find(value => value.kind === kind)
  const references = useQuery({ queryKey: ['face-references', workspaceId], enabled: kind === 'face', queryFn: () => request<{ items: Reference[] }>(`${base(workspaceId)}/face-references`) })
  const detail = useQuery({ queryKey: ['manual-analysis-detail', workspaceId, selected], enabled: !!selected,
    queryFn: () => request<Analysis>(`${base(workspaceId)}/jobs/${encodeURIComponent(selected)}`),
    refetchInterval: query => query.state.data && ['queued', 'running', 'cancelling'].includes(query.state.data.status) ? 1000 : false })
  const refresh = () => { void client.invalidateQueries({ queryKey: ['manual-analysis'] }); void client.invalidateQueries({ queryKey: ['manual-analysis-detail'] }) }
  const choose = (job: Analysis) => { setParams(previous => { const next = new URLSearchParams(previous); next.set('analysis', job.job_id); return next }); refresh() }
  const pendingKey = `clear-manual-analysis:${workspaceId}:${kind}`
  const pending = useRef<unknown>(null)
  useEffect(() => { try { pending.current = JSON.parse(localStorage.getItem(pendingKey) || 'null') } catch { pending.current = null } }, [pendingKey])
  const create = useMutation({ mutationFn: () => {
    if (!pending.current) {
      const inputs = selectedInputs.map(id => media.data?.find(item => item.asset_id === id)).filter((item): item is Asset => !!item)
      pending.current = { request_id: newRequestId(), kind, inputs: inputs.map(item => ({ asset_id: item.asset_id, asset_revision: item.revision, blob_hash: item.blob_hash })),
        options: { device, action: kind === 'face' ? action : 'analyze', face_indices: faceIndices, top_k: 5, threshold: .363, expected_subject: subject, channel: kind === 'audio' ? channel : null } }
      localStorage.setItem(pendingKey, JSON.stringify(pending.current))
    }
    return post<Analysis>(`${base(workspaceId)}/jobs`, pending.current)
  }, onSuccess: job => { pending.current = null; localStorage.removeItem(pendingKey); choose(job) } })
  const control = useMutation({ mutationFn: (operation: string) => post<Analysis>(`${base(workspaceId)}/jobs/${selected}/control/${operation}`, {}), onSuccess: refresh })
  const addReference = useMutation({ mutationFn: (face: { input: number; index: number }) => post(`${base(workspaceId)}/face-references`, {
    request_id: newRequestId(), job_id: selected, input_index: face.input, face_index: face.index, label: referenceName.trim(),
  }), onSuccess: () => { setReferenceName(''); void references.refetch() } })
  const removeReference = useMutation({ mutationFn: (id: string) => request(`${base(workspaceId)}/face-references/${id}`, { method: 'DELETE' }), onSuccess: () => { void references.refetch() } })
  const suffixes: Record<Kind, RegExp> = { face: /\.(jpe?g|png|webp|bmp)$/i, audio: /\.(wav|mp3|m4a|flac|ogg|aac|wma)$/i, seal: /\.(jpe?g|png|webp|bmp|pdf)$/i, catnet: /\.jpe?g$/i }
  const files = media.data?.filter(item => suffixes[kind].test(item.original_name)) || []
  const count = kind === 'face' && action === 'compare' ? 2 : 1
  return <>
    <section className="panel mm-input"><div className="section-head"><Title level={3}>选择资料与分析方式</Title><Tag color={capability?.cpu_ready ? 'success' : 'default'}>
      {capability?.cpu_ready ? 'CPU 已验证' : capability?.missing.length ? '资源未齐' : '待验证'}</Tag></div>
      <UploadQueue workspaceId={workspaceId} compact allowLargeAudio={kind === 'audio'} onUploaded={assetId => { void media.refetch(); setSelectedInputs([assetId]) }} />
      {capabilities.isPending ? <Skeleton paragraph={{ rows: 2 }} /> : capability?.missing.length ? <Alert type="warning" title="此能力的模型或运行环境尚未备齐" description="可先导入资料；资源准备完成后开始分析。" /> : null}
      {(media.error || capabilities.error) && <Alert type="error" title={(media.error || capabilities.error)?.message} action={<Button onClick={() => { void media.refetch(); void capabilities.refetch() }}>重试</Button>} />}
      <div className="mm-controls">
        {kind === 'face' && <label>操作<Select value={action} onChange={value => { setAction(value); setSelectedInputs([]) }} options={[
          { value: 'detect', label: '检测人脸与质量' }, { value: 'compare', label: '两张照片比对' }, { value: 'search', label: '检索项目参考库' },
        ]} /></label>}
        {Array.from({ length: count }, (_, index) => <label key={index}>{count === 2 ? `照片 ${index + 1}` : '分析资料'}<Select showSearch optionFilterProp="label"
          value={selectedInputs[index]} placeholder="选择已导入资料" options={files.map(item => ({ value: item.asset_id, label: item.original_name }))}
          onChange={value => setSelectedInputs(previous => { const next = [...previous]; next[index] = value; return next })} /></label>)}
        <label>运行设备<Select value={device} onChange={setDevice} options={[{ value: 'cpu', label: 'CPU' }, { value: 'cuda', label: 'GPU', disabled: !capability?.gpu_ready }]} /></label>
        {kind === 'audio' && <label>分析声道<Select value={channel === null ? 'mix' : 'selected'} onChange={value => setChannel(value === 'mix' ? null : 0)} options={[{ value: 'mix', label: '混合全部声道' }, { value: 'selected', label: '选择原始声道' }]} />{channel !== null && <InputNumber aria-label="原始声道序号" min={1} max={64} value={channel + 1} onChange={value => setChannel(Math.max(0, Number(value || 1) - 1))} />}</label>}
        {kind === 'face' && action !== 'detect' && Array.from({ length: count }, (_, index) => <label key={index}>照片 {index + 1} 的人脸序号<InputNumber min={1} max={100} value={faceIndices[index] + 1}
          onChange={value => setFaceIndices(previous => previous.map((old, i) => i === index ? Number(value || 1) - 1 : old))} /></label>)}
        {kind === 'seal' && <label>待核对主体名称<Input value={subject} onChange={event => setSubject(event.target.value)} placeholder="可选：合同主体或供应商名称" maxLength={256} /></label>}
      </div>
      <Space wrap><Button type="primary" loading={create.isPending} disabled={!pending.current && (!capability?.cpu_ready || selectedInputs.filter(Boolean).length !== count)} onClick={() => create.mutate()}>
        {pending.current ? '核实原分析请求' : `开始${names[kind]}`}</Button><Button onClick={() => void media.refetch()}>刷新资料</Button></Space>
      {kind === 'face' && <Paragraph type="secondary">多人照片可先检测，再按标注序号选择人脸进行比对或检索。相似分数需人工核对。</Paragraph>}
      {kind === 'audio' && <Paragraph type="secondary">单文件同时满足不超过 4 小时、4 GiB。长录音分块保存进度，可在中断后恢复；说话人分组需要人工回听核对。</Paragraph>}
      {create.error && <Alert type="error" title={create.error.message} action={<Button onClick={() => { pending.current = null; localStorage.removeItem(pendingKey); create.reset() }}>修改分析请求</Button>} />}
    </section>
    {selected && <section className="panel mm-result"><div className="section-head"><Title level={3}>分析结果与人工复核</Title>
      <Button onClick={() => void detail.refetch()}>刷新结果</Button></div>
      {detail.isPending && <Skeleton active paragraph={{ rows: 4 }} />}
      {detail.error && <Alert type="error" title={detail.error.message} />}
      {detail.data && <><Space wrap><Tag>{stateNames[detail.data.status] || detail.data.status}</Tag><Text>{({ queued: '等待执行', prepare: '准备资料', completed: '分析完成', failed: '执行失败', cancelled: '已取消', interrupted: '上次执行中断', waiting_gpu: '等待 GPU', resuming: '恢复处理', yielding_gpu: '保存进度并让出 GPU' } as Record<string, string>)[detail.data.stage] || detail.data.stage}</Text>
        {['queued', 'running', 'cancelling'].includes(detail.data.status) && <Button danger loading={control.isPending} onClick={() => control.mutate('cancel')}>取消任务</Button>}
        {['failed', 'cancelled', 'needs_verification'].includes(detail.data.status) && <Button loading={control.isPending} onClick={() => control.mutate('retry')}>{kind === 'audio' ? '核验进度并恢复处理' : '重试此任务'}</Button>}
        {['failed', 'cancelled', 'needs_verification'].includes(detail.data.status) && ['audio', 'catnet'].includes(kind) && <Button loading={control.isPending} onClick={() => control.mutate('retry_cpu')}>改用 CPU 重试</Button>}</Space>
        {detail.data.progress && <Paragraph type="secondary">已处理 {clock(detail.data.progress.processed_ms || 0)} / {clock(detail.data.progress.duration_ms || 0)} · {detail.data.progress.completed_chunks || 0} / {detail.data.progress.total_chunks || 0} 块{detail.data.actual_device ? ` · 实际设备 ${detail.data.actual_device}` : ''}</Paragraph>}
        <Paragraph>{detail.data.inputs.map(item => item.original_name).join(' / ')} · 复核版本 {detail.data.revision}</Paragraph>
        {detail.data.error && <Alert type="error" title="分析未完成" description={<details><summary>查看失败原因</summary><pre className="mm-log">{detail.data.error}</pre></details>} />}
        {detail.data.kind === kind && detail.data.result && <ResultView key={`${selected}:${detail.data.revision}`} item={detail.data} workspaceId={workspaceId} refresh={refresh}
          referenceName={referenceName} setReferenceName={setReferenceName} onReference={(input, index) => addReference.mutate({ input, index })} referenceBusy={addReference.isPending} />}
      </>}
      {(control.error || addReference.error) && <Alert type="error" title={(control.error || addReference.error)?.message} />}
    </section>}
    {kind === 'face' && <section className="panel"><div className="section-head"><Title level={3}>当前项目参考库</Title><Text type="secondary">手动登记 · 最多 1000 张参考人脸</Text></div>
      {references.error && <Alert type="error" title={references.error.message} />}
      <Table<Reference> size="small" rowKey="reference_id" loading={references.isPending} dataSource={references.data?.items || []} pagination={{ pageSize: 10 }} columns={[
        { title: '参考名称', dataIndex: 'label' }, { title: '来源资料', render: (_, row) => row.source.original_name },
        { title: '操作', render: (_, row) => <Button danger size="small" loading={removeReference.isPending} onClick={() => removeReference.mutate(row.reference_id)}>移出参考库</Button> },
      ]} />{removeReference.error && <Alert type="error" title={removeReference.error.message} />}</section>}
    <section className="panel"><div className="section-head"><Title level={3}>分析记录</Title><Button onClick={refresh}>刷新记录</Button></div>
      {list.error && <Alert type="error" title={list.error.message} />}
      <Table<Analysis> size="small" rowKey="job_id" loading={list.isPending} dataSource={list.data?.items || []} pagination={{ current: page, pageSize: 10, total: list.data?.total, onChange: setPage, showSizeChanger: false }} columns={[
        { title: '资料', render: (_, row) => row.inputs.map(item => item.original_name).join(' / ') },
        { title: '状态', render: (_, row) => <Tag>{stateNames[row.status] || row.status}</Tag> },
        { title: '人工复核', render: (_, row) => row.review ? `已保存 · v${row.revision}` : '待复核' },
        { title: '操作', render: (_, row) => <Button type="link" onClick={() => choose(row)}>查看结果</Button> },
      ]} />
    </section>
  </>
}

export function ResultView({ item, workspaceId, refresh, referenceName, setReferenceName, onReference, referenceBusy }: {
  item: Analysis; workspaceId: string; refresh: () => void; referenceName: string; setReferenceName: (value: string) => void;
  onReference: (input: number, index: number) => void; referenceBusy: boolean;
}) {
  const result = item.result!, audioRef = useRef<HTMLAudioElement>(null), navigate = useNavigate()
  const catGeometry = readGeometry(result.processing?.geometry)
  const displayPreview = result.processing?.display_preview
  const displayHeatmap = result.processing?.display_heatmap
  const catCoordinatesReady = !!catGeometry && catGeometry.persisted_region_coordinate_system === 'stored_pixels'
    && !!displayPreview && !!displayHeatmap && !!result.output_files?.[displayPreview] && !!result.output_files?.[displayHeatmap]
  const catWarning = !catCoordinatesReady ? '旧版结果或预览缺少可核验的坐标元数据，保留历史原图与原保存坐标；热图仅独立查看，不能确认配准。' : undefined
  const audioRows = (result.segments || []).map((row, index) => ({ ...row, review_index: index }))
  const sealRows = (result.seals || []).map((row, index) => ({ ...row, review_index: index }))
  const draftKey = `clear-manual-review:${workspaceId}:${item.job_id}:${item.revision}`
  const [draft] = useState(() => { try { return JSON.parse(localStorage.getItem(draftKey) || 'null') as Analysis['review'] } catch { return null } })
  const [edits, setEdits] = useState<Edit[]>(draft?.edits || item.review?.edits || [])
  const [marks, setMarks] = useState<Mark[]>(draft?.regions || item.review?.regions || [])
  const [note, setNote] = useState(draft?.note || item.review?.note || ''), [actor, setActor] = useState(draft?.actor || item.review?.actor || '')
  const [assessment, setAssessment] = useState(draft?.assessment || item.review?.assessment || 'inconclusive')
  const [previewPage, setPreviewPage] = useState(1)
  const [draftError, setDraftError] = useState('')
  const reviewIntent = useRef<unknown>((() => { try { return JSON.parse(localStorage.getItem(`${draftKey}:request`) || 'null') } catch { return null } })())
  useEffect(() => { try { localStorage.setItem(draftKey, JSON.stringify({ actor, note, assessment, edits, regions: marks })); setDraftError('') } catch { setDraftError('浏览器草稿空间不足，请保存复核后再关闭页面。') } }, [draftKey, actor, note, assessment, edits, marks])
  const url = `${API}${base(workspaceId)}/jobs/${item.job_id}`
  const file = (name: string) => `${url}/files/${name}`
  const review = useMutation({ mutationFn: () => {
    reviewIntent.current ||= { request_id: newRequestId(), expected_revision: item.revision, actor: actor.trim(), note: note.trim(), assessment, edits, regions: marks }
    try { localStorage.setItem(`${draftKey}:request`, JSON.stringify(reviewIntent.current)) } catch { setDraftError('请求草稿未能保存在浏览器中，请先确认保存结果再关闭页面。') }
    return post(`${base(workspaceId)}/jobs/${item.job_id}/review`, reviewIntent.current)
  }, onSuccess: () => { localStorage.removeItem(`${draftKey}:request`); localStorage.removeItem(draftKey); refresh() } })
  const collect = useMutation({ mutationFn: () => post<{ files: { name: string; artifact_id: string }[] }>(`${base(workspaceId)}/jobs/${item.job_id}/collect`, {}) })
  const toData = useMutation({ mutationFn: () => post<{ location: string }>(`${base(workspaceId)}/jobs/${item.job_id}/to-data`, {}), onSuccess: value => navigate(value.location) })
  const update = (row: Segment | Seal, field: string, value: string) => setEdits(previous => {
    const matches = (edit: Edit) => edit.row_id ? edit.row_id === row.row_id : edit.index === row.review_index
    const current = previous.find(matches) || { index: row.review_index, row_id: row.row_id }
    return [...previous.filter(edit => !matches(edit)), { ...current, row_id: row.row_id, index: row.review_index, [field]: value }]
  })
  const merged = <T extends Segment | Seal>(row: T) => ({ ...row, ...edits.find(edit => edit.row_id ? edit.row_id === row.row_id : edit.index === row.review_index) })
  const seek = (milliseconds: number) => { if (audioRef.current) { audioRef.current.currentTime = milliseconds / 1000; void audioRef.current.play().catch(() => {}) } }
  return <div className="mm-result-body">
    <Space wrap><Tag color="processing">{outcomes[result.outcome] || result.outcome}</Tag><Text>{result.algorithm}</Text><Text type="secondary">{result.device.toUpperCase()} · {result.elapsed_seconds.toFixed(1)} 秒</Text></Space>
    {item.kind === 'face' && <>
      {result.comparison && <div className="mm-score"><span>两图余弦相似度</span><strong>{result.comparison.cosine_similarity.toFixed(4)}</strong><Text type="secondary">{result.comparison.meets_reference_threshold ? '达到参考阈值，需人工核对' : '未达到参考阈值'}</Text></div>}
      <label className="mm-reference-label">参考人脸名称<Input placeholder="登记前填写姓名或参考标签" value={referenceName} maxLength={128} onChange={event => setReferenceName(event.target.value)} /></label>
      <div className="mm-forensics-images">{result.images?.map((image, index) => <figure key={index}><figcaption>照片 {index + 1} · 原图人脸位置</figcaption><RegionReview image={file(image.preview)} width={image.width} height={image.height} marks={[]} boxes={image.faces.map(face => ({ box: face.box, label: String(face.face_index + 1) }))} /></figure>)}</div>
      <div className="mm-faces">{result.images?.flatMap((image, input) => image.faces.map(face => <figure key={`${input}:${face.face_index}`}>
        <Image width={112} src={file(face.crop)} alt={`照片${input + 1}人脸${face.face_index + 1}`} />
        <figcaption>照片 {input + 1} · 人脸 {face.face_index + 1}<br /><Text type={face.quality.usable ? 'secondary' : 'warning'}>{face.quality.usable ? '可用于人工比对' : face.quality.note}</Text></figcaption>
        <Button size="small" loading={referenceBusy} disabled={!face.quality.usable || !referenceName.trim()} onClick={() => onReference(input, face.face_index)}>加入项目参考库</Button>
      </figure>))}</div>
      {!result.images?.some(image => image.faces.length) && <Empty description="没有检出可用人脸，可更换清晰照片后重试" />}
      {result.candidates && <Table size="small" rowKey="reference_id" pagination={false} dataSource={result.candidates} columns={[
        { title: '参考候选', dataIndex: 'label' }, { title: '余弦相似度', dataIndex: 'score', render: (value: number) => value.toFixed(4) },
        { title: '参考阈值', dataIndex: 'meets_reference_threshold', render: (value: boolean) => value ? '达到，待人工核对' : '未达到' },
      ]} />}
    </>}
    {item.kind === 'audio' && <>
      <audio controls preload="metadata" ref={audioRef} src={file('playback.wav')} aria-label="分析音轨播放" />
      <Paragraph type="secondary">分析音轨：16kHz 单声道，时间对应原音起点。原件为 {result.audio?.original_sample_rate}Hz / {result.audio?.original_channels} 声道。
        <a href={`${API}/workspaces/${workspaceId}/assets/${item.inputs[0].asset_id}/content?revision=${item.inputs[0].asset_revision}&download=true`}>下载原音</a></Paragraph>
      <div className="mm-audio-images"><Text>振幅波形（−1 至 1）</Text><img src={file('waveform.png')} alt="录音全长波形，横轴从起点至结束" /><div className="mm-axis"><span>0:00</span><span>{clock(result.audio?.duration_ms || 0)}</span></div><Text>频谱：上方 8kHz，下方 0Hz</Text><img src={file('spectrogram.png')} alt="录音全长频谱，纵轴由高频至低频" /><div className="mm-axis"><span>0:00</span><span>{clock(result.audio?.duration_ms || 0)}</span></div></div>
      <Table<Segment> rowKey="row_id" size="small" pagination={{ pageSize: 20, showSizeChanger: false }} dataSource={audioRows} columns={[
        { title: '原音时间', width: 140, render: (_, row) => <Button type="link" onClick={() => seek(row.start_ms)}>{clock(row.start_ms)}–{clock(row.end_ms)}</Button> },
        { title: '说话人', width: 150, render: (_, row) => <Input aria-label={`第${row.review_index + 1}段说话人`} value={merged(row).speaker} onChange={event => update(row, 'speaker', event.target.value)} /> },
        { title: '转写与人工校正', render: (_, row) => <Input.TextArea aria-label={`第${row.review_index + 1}段转写`} autoSize value={merged(row).text} onChange={event => update(row, 'text', event.target.value)} /> },
      ]} />
    </>}
    {item.kind === 'seal' && <>
      <Paragraph>逐章校正文字和主体字段。保存复核后可直接转入 ClearData，与另一份主数据表核对。</Paragraph>
      <Select aria-label="印章原页" value={previewPage} onChange={setPreviewPage} options={result.pages?.map(page => ({ value: page.page_number, label: `原件第 ${page.page_number} 页` }))} />
      {result.pages?.filter(page => page.page_number === previewPage).map(page => <RegionReview key={page.page_number} image={file(page.preview)} width={page.width} height={page.height} marks={[]}
        boxes={(result.seals || []).flatMap((seal, index) => seal.page_number === page.page_number && seal.box ? [{ box: seal.box, label: `#${index + 1}` }] : [])} />)}
      <a href={`${API}/workspaces/${workspaceId}/assets/${item.inputs[0].asset_id}/content?revision=${item.inputs[0].asset_revision}`} target="_blank" rel="noreferrer">打开完整原件对照</a>
      <Table<Seal> rowKey="row_id" size="small" pagination={{ pageSize: 10, showSizeChanger: false }} dataSource={sealRows} columns={[
        { title: '原页', dataIndex: 'page_number', width: 70 },
        ...(['text', 'subject', 'number'] as const).map(field => ({ title: { text: '章内文字', subject: '主体名称', number: '编号' }[field], render: (_: unknown, row: Seal) => <Input.TextArea autoSize aria-label={`印章${row.review_index + 1}${field}`} value={merged(row)[field]} onChange={event => update(row, field, event.target.value)} /> })),
        { title: '自动核对', dataIndex: 'subject_check', render: (value: string) => ({ contains: '包含目标名称', needs_review: '有差异，待人工核对', not_requested: '未指定核对主体' })[value] || value },
      ]} />
      <Button disabled={!item.review} loading={toData.isPending} onClick={() => toData.mutate()}>将已保存印章字段转入 ClearData</Button>
      {toData.error && <Alert type="error" title={toData.error.message} />}
    </>}
    {item.kind === 'catnet' && <><RegionReview image={file(catCoordinatesReady ? displayPreview! : 'preview.png')}
      width={result.width || 1000} height={result.height || 1000} marks={marks} onChange={setMarks}
      forensic={{ heatmap: file(catCoordinatesReady ? displayHeatmap! : 'heatmap.png'), geometry: catCoordinatesReady ? catGeometry : null,
        registered: catCoordinatesReady, warning: catWarning }} />
      <Link to={`/apps/forensics?workspace=${workspaceId}&asset=${item.inputs[0].asset_id}`}>使用经典取证工具继续核查</Link></>}
    <ul className="mm-limitations">{result.limitations.map((value, index) => <li key={index}>{value}</li>)}</ul>
    <div className="mm-review"><Title level={4}>保存人工复核</Title><div className="mm-controls">
      <label>复核人<Input value={actor} maxLength={128} onChange={event => setActor(event.target.value)} /></label>
      <label>复核结论<Select value={assessment} onChange={setAssessment} options={[{ value: 'confirmed', label: '已人工确认所记录结果' }, { value: 'needs_investigation', label: '需要进一步调查' }, { value: 'inconclusive', label: '无法判断' }]} /></label>
    </div><Input.TextArea value={note} maxLength={10000} autoSize={{ minRows: 2 }} placeholder="填写复核依据、修改说明或待查事项" onChange={event => setNote(event.target.value)} />
      {draftError && <Alert type="warning" title={draftError} />}
      <Space wrap><Button type="primary" loading={review.isPending} disabled={!actor.trim() || !note.trim()} onClick={() => review.mutate()}>保存复核与校正</Button>
        <a href={`${url}/download`}>下载已保存版本（含 JSON / CSV / 字幕）</a><Button disabled={!item.review} loading={collect.isPending} onClick={() => collect.mutate()}>归集到成果中心</Button>
        <Link to={`/artifacts?workspace=${workspaceId}`}>查看成果</Link></Space>
      {(review.error || collect.error) && <Alert type="error" title={(review.error || collect.error)?.message} />}
      {review.error && <Button onClick={() => { reviewIntent.current = null; localStorage.removeItem(`${draftKey}:request`); review.reset(); refresh() }}>刷新版本并修改复核请求</Button>}
      {collect.data && <Alert type="success" title={`已归集 ${collect.data.files.length} 份固定成果`} description={<Link to={`/audit?workspace=${workspaceId}`}>在审计项目的调查事项中引用此成果</Link>} />}
    </div>
    <details><summary>模型、来源及结果身份</summary><pre className="mm-log">{JSON.stringify({ result_hash: item.result_hash, inputs: item.inputs, result }, null, 2)}</pre></details>
  </div>
}

export function MultimodalWorkbench({ kind }: { kind: Kind }) {
  const workspaceId = useWorkspaceId(), [params] = useSearchParams()
  return <div className="multimodal-workbench"><div className="page-heading"><Text className="eyebrow">CLEAR / MANUAL ANALYSIS</Text><Title level={1}>{names[kind]}</Title><Paragraph>{descriptions[kind]}</Paragraph></div>
    {workspaceId ? <Workspace key={`${workspaceId}:${kind}`} workspaceId={workspaceId} kind={kind} /> : <Workspaces continueTo={routes[kind]} />}
    <div className="back-link"><Link to={`/apps${params.size ? `?${params}` : ''}`}>← 返回手动工具</Link></div>
  </div>
}
