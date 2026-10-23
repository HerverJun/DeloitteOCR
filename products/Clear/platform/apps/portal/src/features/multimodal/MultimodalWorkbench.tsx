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
import { ManualReturn } from '../agent/ManualReturn'
import { AigcResultView, type AigcImageResult } from './AigcResultView'
import './multimodal.css'

const { Title, Paragraph, Text } = Typography
type Kind = 'face' | 'audio' | 'seal' | 'catnet' | 'document_forensics' | 'aigc_image'
type Capability = { kind: Kind; title: string; state: string; cpu_ready: boolean; gpu_ready: boolean; missing: string[] }
type Face = { face_index: number; box: number[]; detection_score: number; crop: string; quality: { usable: boolean; note: string } }
type Segment = { row_id: string; review_index: number; start_ms: number; end_ms: number; text: string; speaker: string }
type Seal = { row_id: string; review_index: number; page_number: number; box?: number[]; text: string; subject: string; number: string; subject_check: string }
type Result = { outcome: string; algorithm: string; device: string; elapsed_seconds: number; limitations: string[];
  aigc_image?: AigcImageResult; model?: Record<string, unknown>; performance?: Record<string, unknown>;
  engine?: string; detector?: { version?: string; commit?: string; calibration?: boolean };
  assessment?: { state: string; label: string; reason: string; calibration?: boolean; calibration_id?: string };
  deep_analysis?: { status: string; reason?: string };
  candidate_regions?: { box: Mark['box']; pixel_box?: number[]; area?: number; score?: number }[];
  classical?: { evidence: { tool: string; status: string; map_path?: string; reason?: string }[] };
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
type AnalysisSnapshot = { snapshot_id: string; revision: number; review_revision: number; review_state: string; snapshot_hash: string; analysis: Analysis }
const names: Record<Kind, string> = { face: '人脸比对', audio: '音频分析', seal: '印章识别', catnet: 'SAFIRE 深度定位', document_forensics: '文档篡改排查', aigc_image: '整图 AI 生成检测' }
const forensicLabels: Record<string, string> = { metadata: '文件画像', noise: '噪声残差', frequency: '高频分布', jpeg: 'JPEG 重压缩', jpeg_grid: 'JPEG 块网格', copy_move: '重复区域' }
const aigcEngineLabels: Record<string, string> = { community_forensics: 'Community Forensics', community_forensics384: 'Community Forensics', spai: 'SPAI', univfd: 'UniversalFakeDetect' }
const isDeepKind = (kind: Kind) => kind === 'catnet' || kind === 'document_forensics'
const prefersGpu = (kind: Kind) => isDeepKind(kind) || kind === 'aigc_image'
const descriptions: Record<Kind, string> = {
  face: '检测照片中的人脸，进行两图比对或检索当前项目参考库，保存人工核对结果。',
  audio: '回听录音、查看波形与频谱，校正带时间定位和匿名说话人的中文转写。',
  seal: '定位文档中的多个印章，校正章内文字与编号，再核对主体名称。',
  catnet: '使用 SAFIRE 定位图像可疑区域，支持 JPEG、PNG，结合原图与经典线索进行人工复核。',
  document_forensics: '使用 SAFIRE 排查票据、合同与截图中的局部改字线索，保存定位区域和人工复核。',
  aigc_image: '独立分析整张图片的生成相关信号，保留原图、模型身份和人工复核。局部区域可继续使用 SAFIRE 定位。',
}
const routes = { face: '/apps/face', audio: '/apps/audio', seal: '/apps/ocr/seals', catnet: '/apps/forensics/deep', document_forensics: '/apps/forensics/document', aigc_image: '/apps/forensics/aigc' } as const
const outcomes: Record<string, string> = { detected: '已检出人脸', no_face: '未检出可用人脸', compared: '比对已完成', quality_insufficient: '图片质量不足',
  candidates: '已找到参考候选', no_trusted_candidate: '未找到达到参考阈值的候选', transcribed: '转写已完成', no_speech: '未识别出语音', recognized: '已完成印章识别', no_seal: '未检出印章', analyzed: '分析已完成' }
const clock = (ms: number) => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`
const base = (workspace: string) => `/workspaces/${encodeURIComponent(workspace)}/multimodal`

export function MultimodalApplicationCard({ kind }: { kind: 'face' | 'audio' | 'aigc_image' }) {
  const [params] = useSearchParams()
  const capability = useQuery({ queryKey: ['multimodal-capabilities'], queryFn: () => request<{ items: Capability[] }>('/multimodal/capabilities') })
  const current = capability.data?.items.find(value => value.kind === kind)
  const to = routes[kind] + (params.size ? `?${params}` : '')
  return <Card className="application-card" title={<ToolIdentity appId={kind} to={to} label={names[kind]} />}>
    <Text className="application-purpose">{kind === 'face' ? '静态照片核对与项目参考库' : kind === 'aigc_image' ? '独立整图分类 · 生成相关信号复核' : '中文录音转写与片段复核'}</Text>
    <Paragraph className="application-description" type="secondary">{descriptions[kind]}</Paragraph>
    <div className="application-state"><Tag color={current?.cpu_ready || kind === 'aigc_image' && current?.gpu_ready ? 'success' : current?.missing.length ? 'warning' : 'default'}>
      {capability.isPending ? '正在核实' : kind === 'aigc_image' && current?.gpu_ready ? 'GPU 推理已验证' : current?.cpu_ready ? 'CPU 推理已验证' : current?.missing.length ? '资源待准备' : kind === 'aigc_image' ? '待 GPU 验证' : '首次使用自动准备'}</Tag>
      <Text type="secondary">{kind === 'face' ? '项目内独立参考库' : kind === 'aigc_image' ? '实验能力 · 独立人工复核' : '原音时间定位与人工修订'}</Text></div>
    {capability.isError && <Alert type="error" title="能力状态暂不可读取" />}
    <Link className="application-actions" to={to}><Button type="primary">打开{names[kind]}</Button></Link>
    <details className="application-details"><summary>查看工具声明</summary><Paragraph>{kind === 'face' ? 'YuNet / SFace。相似度与候选名称均需人工核对；静态照片分析不提供活体结论。' : kind === 'aigc_image' ? '保留模型、预处理、校准和原始分数记录。检测信号需结合来源核查，未发现信号不能证明来源真实；SAFIRE 局部定位独立运行。' : 'Paraformer / FSMN-VAD / ct-punc / CAM++。匿名说话人由人工命名，引用片段前需回听原音。'}</Paragraph></details>
  </Card>
}

function Workspace({ kind, workspaceId }: { kind: Kind; workspaceId: string }) {
  const client = useQueryClient(), [params, setParams] = useSearchParams()
  const [selectedInputs, setSelectedInputs] = useState<string[]>(() => params.get('asset') ? [params.get('asset')!] : []), [action, setAction] = useState('detect')
  const [faceIndices, setFaceIndices] = useState([0, 0]), [subject, setSubject] = useState('')
  const [device, setDevice] = useState(prefersGpu(kind) ? 'cuda' : 'cpu'), [referenceName, setReferenceName] = useState('')
  const [channel, setChannel] = useState<number | null>(null)
  const [referenceIds, setReferenceIds] = useState<string[]>([])
  const [page, setPage] = useState(1)
  const selected = params.get('analysis') || ''
  const snapshotId = params.get('snapshot'), snapshotHash = params.get('sha256'), snapshotRevision = params.get('revision')
  const list = useQuery({ queryKey: ['manual-analysis', workspaceId, kind, page], queryFn: () => request<{ items: Analysis[]; total: number }>(`${base(workspaceId)}/jobs?kind=${kind}&offset=${(page - 1) * 10}&limit=10`),
    refetchInterval: query => query.state.data?.items.some(item => ['queued', 'running', 'cancelling'].includes(item.status)) ? 1500 : false })
  const media = useQuery({ queryKey: ['multimodal-assets', workspaceId], queryFn: ({ signal }) => assets(workspaceId, signal) })
  const capabilities = useQuery({ queryKey: ['multimodal-capabilities'], queryFn: () => request<{ items: Capability[] }>('/multimodal/capabilities') })
  const capability = capabilities.data?.items.find(value => value.kind === kind)
  const references = useQuery({ queryKey: ['face-references', workspaceId], enabled: kind === 'face', queryFn: () => request<{ items: Reference[] }>(`${base(workspaceId)}/face-references`) })
  const detail = useQuery({ queryKey: ['manual-analysis-detail', workspaceId, selected], enabled: !!selected && !snapshotId,
    queryFn: () => request<Analysis>(`${base(workspaceId)}/jobs/${encodeURIComponent(selected)}`),
    refetchInterval: query => query.state.data && ['queued', 'running', 'cancelling'].includes(query.state.data.status) ? 1000 : false })
  const snapshot = useQuery({ queryKey: ['manual-analysis-snapshot', workspaceId, selected, snapshotId], enabled: !!selected && !!snapshotId,
    queryFn: () => request<AnalysisSnapshot>(`${base(workspaceId)}/jobs/${encodeURIComponent(selected)}/snapshots/${encodeURIComponent(snapshotId!)}`) })
  const refresh = () => { void client.invalidateQueries({ queryKey: ['manual-analysis'] }); void client.invalidateQueries({ queryKey: ['manual-analysis-detail'] }) }
  const choose = (job: Analysis) => { setParams(previous => { const next = new URLSearchParams(previous); next.set('analysis', job.job_id); ['snapshot', 'sha256', 'revision'].forEach(key => next.delete(key)); return next }); refresh() }
  const pendingKey = `clear-manual-analysis:${workspaceId}:${kind}`
  const pending = useRef<unknown>(null)
  useEffect(() => { try { pending.current = JSON.parse(localStorage.getItem(pendingKey) || 'null') } catch { pending.current = null } }, [pendingKey])
  const create = useMutation({ mutationFn: () => {
    if (!pending.current) {
      const inputs = selectedInputs.map(id => media.data?.find(item => item.asset_id === id)).filter((item): item is Asset => !!item)
      pending.current = { request_id: newRequestId(), kind, inputs: inputs.map(item => ({ asset_id: item.asset_id, asset_revision: item.revision, blob_hash: item.blob_hash })),
        options: kind === 'aigc_image' ? { device, action: 'analyze' } : { device, action: kind === 'face' ? action : 'analyze', face_indices: faceIndices, ...(kind === 'face' && action === 'search' ? { reference_ids: referenceIds } : {}), top_k: 5, threshold: .363, expected_subject: subject, channel: kind === 'audio' ? channel : null } }
      localStorage.setItem(pendingKey, JSON.stringify(pending.current))
    }
    return post<Analysis>(`${base(workspaceId)}/jobs`, pending.current)
  }, onSuccess: job => { pending.current = null; localStorage.removeItem(pendingKey); choose(job) } })
  const control = useMutation({ mutationFn: (operation: string) => post<Analysis>(`${base(workspaceId)}/jobs/${selected}/control/${operation}`, {}), onSuccess: refresh })
  const addReference = useMutation({ mutationFn: (face: { input: number; index: number }) => post(`${base(workspaceId)}/face-references`, {
    request_id: newRequestId(), job_id: selected, input_index: face.input, face_index: face.index, label: referenceName.trim(),
  }), onSuccess: () => { setReferenceName(''); void references.refetch() } })
  const removeReference = useMutation({ mutationFn: (id: string) => request(`${base(workspaceId)}/face-references/${id}`, { method: 'DELETE' }), onSuccess: () => { void references.refetch() } })
  const suffixes: Record<Kind, RegExp> = { face: /\.(jpe?g|png|webp|bmp)$/i, audio: /\.(wav|mp3|m4a|flac|ogg|aac|wma)$/i, seal: /\.(jpe?g|png|webp|bmp|pdf)$/i, catnet: /\.(jpe?g|png|webp|bmp|tiff?)$/i, document_forensics: /\.(jpe?g|png|webp|bmp|tiff?)$/i, aigc_image: /\.(jpe?g|png|webp|bmp|tiff?)$/i }
  const files = media.data?.filter(item => suffixes[kind].test(item.original_name)) || []
  const count = kind === 'face' && action === 'compare' ? 2 : 1
  const cpuAvailable = !!capability && (capability.cpu_ready || !isDeepKind(kind) && kind !== 'aigc_image' && !capability.missing.length)
  const deviceReady = device === 'cuda' ? capability?.gpu_ready : cpuAvailable
  return <>
    <details className="panel mm-input" open={!selected}><summary className="mm-input-heading"><span>选择资料与分析方式</span><Tag color={deviceReady ? 'success' : 'default'}>
      {deviceReady ? device === 'cpu' && !capability?.cpu_ready ? 'CPU · 首次使用自动准备' : `${device === 'cuda' ? 'GPU' : 'CPU'} 已就绪` : capability?.missing.length ? '资源未齐' : '待验证'}</Tag></summary>
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
        <label>运行设备<Select value={device} onChange={setDevice} options={[{ value: 'cpu', label: 'CPU', disabled: prefersGpu(kind) && !capability?.cpu_ready }, { value: 'cuda', label: 'GPU', disabled: !capability?.gpu_ready }]} /></label>
        {kind === 'audio' && <label>分析声道<Select value={channel === null ? 'mix' : 'selected'} onChange={value => setChannel(value === 'mix' ? null : 0)} options={[{ value: 'mix', label: '混合全部声道' }, { value: 'selected', label: '选择原始声道' }]} />{channel !== null && <InputNumber aria-label="原始声道序号" min={1} max={64} value={channel + 1} onChange={value => setChannel(Math.max(0, Number(value || 1) - 1))} />}</label>}
        {kind === 'face' && action !== 'detect' && Array.from({ length: count }, (_, index) => <label key={index}>照片 {index + 1} 的人脸序号<InputNumber min={1} max={100} value={faceIndices[index] + 1}
          onChange={value => setFaceIndices(previous => previous.map((old, i) => i === index ? Number(value || 1) - 1 : old))} /></label>)}
        {kind === 'face' && action === 'search' && <label>检索参考范围<Select mode="multiple" aria-label="检索参考范围" value={referenceIds} onChange={setReferenceIds} placeholder="明确选择本次可用的参考"
          loading={references.isPending} options={references.data?.items.map(reference => ({ value: reference.reference_id, label: `${reference.label} · ${reference.source.original_name}` }))} /></label>}
        {kind === 'seal' && <label>待核对主体名称<Input value={subject} onChange={event => setSubject(event.target.value)} placeholder="可选：合同主体或供应商名称" maxLength={256} /></label>}
      </div>
      <Space wrap><Button type="primary" loading={create.isPending} disabled={!pending.current && (!deviceReady || selectedInputs.filter(Boolean).length !== count || kind === 'face' && action === 'search' && !referenceIds.length)} onClick={() => create.mutate()}>
        {pending.current ? '核实原分析请求' : `开始${names[kind]}`}</Button><Button onClick={() => void media.refetch()}>刷新资料</Button></Space>
      {kind === 'face' && <Paragraph type="secondary">多人照片可先检测，再按标注序号选择人脸进行比对或检索。相似分数需人工核对。</Paragraph>}
      {kind === 'audio' && <Paragraph type="secondary">单文件同时满足不超过 4 小时、4 GiB。长录音分块保存进度，可在中断后恢复；说话人分组需要人工回听核对。</Paragraph>}
      {isDeepKind(kind) && <Paragraph type="secondary">支持单帧 JPEG、PNG、WebP、BMP、TIFF，单图不超过 32 MiB、1200 万像素。定位线索尚未独立校准，区域需结合原件复核。</Paragraph>}
      {kind === 'aigc_image' && <Paragraph type="secondary">实验能力。支持单帧 JPEG、PNG、WebP、BMP、TIFF，单图不超过 32 MiB、1200 万像素。分数表示生成相关信号强弱，压缩、截图和文档版式可能影响结果。</Paragraph>}
      {isDeepKind(kind) && !capabilities.isPending && !deviceReady && !capability?.missing.length && <Alert type="warning" title="当前设备尚未通过 SAFIRE 推理验证" description="GPU 不可用时不能开始深度定位，请核查运行设备。" />}
      {kind === 'aigc_image' && !capabilities.isPending && !deviceReady && !capability?.missing.length && <Alert type="warning" title="当前设备尚未通过整图生成检测推理验证" description="请核查运行设备，或选择已验证的设备。" />}
      {create.error && <Alert type="error" title={create.error.message} action={<Button onClick={() => { pending.current = null; localStorage.removeItem(pendingKey); create.reset() }}>修改分析请求</Button>} />}
    </details>
    {selected && snapshotId && <section className="panel mm-result" aria-label="固定版本分析结果"><div className="section-head"><Title level={3}>固定版本分析结果</Title>
      <Button onClick={() => { setParams(previous => { const next = new URLSearchParams(previous); ['snapshot', 'sha256', 'revision'].forEach(key => next.delete(key)); return next }) }}>打开当前结果复核</Button></div>
      <Paragraph type="secondary">此引用固定到当时的结果与复核版本。后续重试和人工修改不会替换这里的内容。</Paragraph>
      {snapshot.isPending && <Skeleton active paragraph={{ rows: 4 }} />}{snapshot.error && <Alert type="error" title="固定结果不可读取" description={snapshot.error.message} />}
      {snapshot.data && (snapshot.data.snapshot_id !== snapshotId || snapshotHash && snapshot.data.snapshot_hash !== snapshotHash || snapshotRevision && String(snapshot.data.revision) !== snapshotRevision ? <Alert type="error" title="结果身份核验不一致" description="停止显示结果，请从原任务重新核对引用。" /> : snapshot.data.analysis.kind !== kind ? <Alert type="error" title="结果类型与当前工具不一致" /> : <>
        <Tag color={snapshot.data.review_revision ? 'success' : 'warning'}>{snapshot.data.review_revision ? `人工复核已保存 · v${snapshot.data.review_revision}` : '未复核 · 仅供草稿分析'}</Tag>
        {snapshot.data.analysis.result && <ResultView key={snapshotId} item={snapshot.data.analysis} workspaceId={workspaceId} refresh={refresh} referenceName="" setReferenceName={() => {}} onReference={() => {}} referenceBusy={false} snapshotId={snapshotId} readOnly />}
      </>)}
    </section>}
    {selected && !snapshotId && <section className="panel mm-result"><div className="section-head"><Title level={3}>分析结果与人工复核</Title>
      <Button onClick={() => void detail.refetch()}>刷新结果</Button></div>
      {detail.isPending && <Skeleton active paragraph={{ rows: 4 }} />}
      {detail.error && <Alert type="error" title={detail.error.message} />}
      {detail.data && <><Space wrap><Tag>{stateNames[detail.data.status] || detail.data.status}</Tag><Text>{({ queued: '等待执行', prepare: '准备资料', completed: '分析完成', failed: '执行失败', cancelled: '已取消', interrupted: '上次执行中断', waiting_gpu: '等待 GPU', resuming: '恢复处理', yielding_gpu: '保存进度并让出 GPU' } as Record<string, string>)[detail.data.stage] || detail.data.stage}</Text>
        {['queued', 'running', 'cancelling'].includes(detail.data.status) && <Button danger loading={control.isPending} onClick={() => control.mutate('cancel')}>取消任务</Button>}
        {['failed', 'cancelled', 'needs_verification'].includes(detail.data.status) && <Button loading={control.isPending} onClick={() => control.mutate('retry')}>{kind === 'audio' ? '核验进度并恢复处理' : '重试此任务'}</Button>}
        {['failed', 'cancelled', 'needs_verification'].includes(detail.data.status) && (kind === 'audio' || prefersGpu(kind) && capability?.cpu_ready) && <Button loading={control.isPending} onClick={() => control.mutate('retry_cpu')}>改用 CPU 重试</Button>}</Space>
        {detail.data.progress && <Paragraph type="secondary">{kind === 'audio' ? `已处理 ${clock(detail.data.progress.processed_ms || 0)} / ${clock(detail.data.progress.duration_ms || 0)} · ` : ''}{detail.data.progress.completed_chunks || 0} / {detail.data.progress.total_chunks || 0} 块{detail.data.actual_device ? ` · 实际设备 ${detail.data.actual_device}` : ''}</Paragraph>}
        <Paragraph>{detail.data.inputs.map(item => item.original_name).join(' / ')} · 复核版本 {detail.data.revision}</Paragraph>
        {detail.data.error && <Alert type="error" title={prefersGpu(kind) ? '无法判断：分析未完成' : '分析未完成'} description={<details><summary>查看失败原因</summary><pre className="mm-log">{detail.data.error}</pre></details>} />}
        {detail.data.kind === kind && detail.data.result && <ResultView key={`${selected}:${detail.data.revision}`} item={detail.data} workspaceId={workspaceId} refresh={refresh}
          referenceName={referenceName} setReferenceName={setReferenceName} onReference={(input, index) => addReference.mutate({ input, index })} referenceBusy={addReference.isPending} />}
      </>}
      {(control.error || addReference.error) && <Alert type="error" title={(control.error || addReference.error)?.message} />}
    </section>}
    <ManualReturn taskId={params.get('task_return') || undefined} workspaceId={workspaceId} />
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

export function ResultView({ item, workspaceId, refresh, referenceName, setReferenceName, onReference, referenceBusy, snapshotId, readOnly = false }: {
  item: Analysis; workspaceId: string; refresh: () => void; referenceName: string; setReferenceName: (value: string) => void;
  onReference: (input: number, index: number) => void; referenceBusy: boolean; snapshotId?: string; readOnly?: boolean;
}) {
  const result = item.result!, audioRef = useRef<HTMLAudioElement>(null), navigate = useNavigate()
  const catGeometry = readGeometry(result.processing?.geometry)
  const displayPreview = result.processing?.display_preview
  const displayHeatmap = result.processing?.display_heatmap
  const deep = isDeepKind(item.kind)
  const safire = result.engine?.toUpperCase() === 'SAFIRE' || /^SAFIRE\b/i.test(result.algorithm)
  const algorithm = result.algorithm || result.engine || (item.kind === 'catnet' ? 'CAT-Net（历史结果）' : '未记录检测器')
  const detectorLabel = item.kind === 'aigc_image' ? aigcEngineLabels[algorithm] || algorithm : algorithm
  const proposed = result.candidate_regions || []
  const classicalOnly = item.kind === 'document_forensics' && result.deep_analysis?.status === 'not_applicable'
  const catCoordinatesReady = !!catGeometry && catGeometry.persisted_region_coordinate_system === catGeometry.source_coordinate_system
    && !!displayPreview && !!result.output_files?.[displayPreview]
    && (classicalOnly || !!displayHeatmap && !!result.output_files?.[displayHeatmap])
  const catWarning = !catCoordinatesReady ? '旧版结果或预览缺少可核验的坐标元数据，保留历史原图与原保存坐标；热图仅独立查看，不能确认配准。' : undefined
  const audioRows = (result.segments || []).map((row, index) => ({ ...row, review_index: index }))
  const sealRows = (result.seals || []).map((row, index) => ({ ...row, review_index: index }))
  const draftKey = `clear-manual-review:${workspaceId}:${item.job_id}:${item.revision}`
  const [draft] = useState(() => { if (readOnly) return null; try { return JSON.parse(localStorage.getItem(draftKey) || 'null') as Analysis['review'] } catch { return null } })
  const [edits, setEdits] = useState<Edit[]>(draft?.edits || item.review?.edits || [])
  const [marks, setMarks] = useState<Mark[]>(draft?.regions || item.review?.regions || [])
  const [note, setNote] = useState(draft?.note || item.review?.note || ''), [actor, setActor] = useState(draft?.actor || item.review?.actor || '')
  const [assessment, setAssessment] = useState(draft?.assessment || item.review?.assessment || 'inconclusive')
  const [previewPage, setPreviewPage] = useState(1)
  const [draftError, setDraftError] = useState('')
  const reviewIntent = useRef<unknown>((() => { try { return JSON.parse(localStorage.getItem(`${draftKey}:request`) || 'null') } catch { return null } })())
  useEffect(() => { if (readOnly) return; try { localStorage.setItem(draftKey, JSON.stringify({ actor, note, assessment, edits, regions: marks })); setDraftError('') } catch { setDraftError('浏览器草稿空间不足，请保存复核后再关闭页面。') } }, [draftKey, actor, note, assessment, edits, marks, readOnly])
  const url = `${API}${base(workspaceId)}/jobs/${item.job_id}${snapshotId ? `/snapshots/${encodeURIComponent(snapshotId)}` : ''}`
  const file = (name: string) => `${url}/files/${name}`
  const review = useMutation({ mutationFn: () => {
    reviewIntent.current ||= { request_id: newRequestId(), expected_revision: item.revision, actor: actor.trim(), note: note.trim(), assessment, edits, regions: marks }
    try { localStorage.setItem(`${draftKey}:request`, JSON.stringify(reviewIntent.current)) } catch { setDraftError('请求草稿未能保存在浏览器中，请先确认保存结果再关闭页面。') }
    return post(`${base(workspaceId)}/jobs/${item.job_id}/review`, reviewIntent.current)
  }, onSuccess: () => { localStorage.removeItem(`${draftKey}:request`); localStorage.removeItem(draftKey); refresh() } })
  const collect = useMutation({ mutationFn: () => post<{ files: { name: string; artifact_id: string }[] }>(`${base(workspaceId)}/jobs/${item.job_id}/collect`, {}) })
  const toData = useMutation({ mutationFn: () => post<{ location: string }>(`${base(workspaceId)}/jobs/${item.job_id}/to-data`, {}), onSuccess: value => navigate(value.location) })
  const draftIntentKey = `clear-seal-draft:${workspaceId}:${item.job_id}:${item.revision}`
  const draftIntent = useRef<{ request_id: string; expected_revision: number } | null>((() => { try {
    const value = JSON.parse(sessionStorage.getItem(draftIntentKey) || 'null')
    return typeof value?.request_id === 'string' && value.expected_revision === item.revision ? value : null
  } catch { return null } })())
  const draftData = useMutation({ mutationFn: () => { draftIntent.current ||= { request_id: newRequestId(), expected_revision: item.revision }
    sessionStorage.setItem(draftIntentKey, JSON.stringify(draftIntent.current))
    return post<{ location: string }>(`${base(workspaceId)}/jobs/${item.job_id}/draft-to-data`, draftIntent.current)
  }, onSuccess: value => { sessionStorage.removeItem(draftIntentKey); navigate(value.location) } })
  const update = (row: Segment | Seal, field: string, value: string) => setEdits(previous => {
    const matches = (edit: Edit) => edit.row_id ? edit.row_id === row.row_id : edit.index === row.review_index
    const current = previous.find(matches) || { index: row.review_index, row_id: row.row_id }
    return [...previous.filter(edit => !matches(edit)), { ...current, row_id: row.row_id, index: row.review_index, [field]: value }]
  })
  const merged = <T extends Segment | Seal>(row: T) => ({ ...row, ...edits.find(edit => edit.row_id ? edit.row_id === row.row_id : edit.index === row.review_index) })
  const seek = (milliseconds: number) => { if (audioRef.current) { audioRef.current.currentTime = milliseconds / 1000; void audioRef.current.play().catch(() => {}) } }
  return <div className="mm-result-body">
    <Space wrap><Tag color="processing">{outcomes[result.outcome] || result.outcome}</Tag><Tag color={item.review ? 'success' : 'warning'}>{item.review ? '人工复核已保存' : '未复核 · 可用于草稿'}</Tag><Text>{detectorLabel}</Text>{result.detector?.version && <Text type="secondary">{result.detector.version}</Text>}<Text type="secondary">{result.device.toUpperCase()} · {result.elapsed_seconds.toFixed(1)} 秒</Text>{safire && <Tag>未校准定位线索</Tag>}</Space>
    {item.kind === 'aigc_image' && <AigcResultView value={result.aigc_image} model={result.model} performance={result.performance}
      preview={file(result.processing?.display_preview || 'preview.png')} sourceName={item.inputs[0]?.original_name || '原图'}
      workspaceId={workspaceId} assetId={item.inputs[0]?.asset_id} />}
    {deep && result.assessment && <Alert type={result.assessment.state === 'review_recommended' ? 'warning' : 'info'} title={result.assessment.label || '无法判断'} description={result.assessment.reason} />}
    {item.kind === 'face' && <>
      {result.comparison && <div className="mm-score"><span>两图余弦相似度</span><strong>{result.comparison.cosine_similarity.toFixed(4)}</strong><Text type="secondary">{result.comparison.meets_reference_threshold ? '达到参考阈值，需人工核对' : '未达到参考阈值'}</Text></div>}
      {!readOnly && <label className="mm-reference-label">参考人脸名称<Input placeholder="登记前填写姓名或参考标签" value={referenceName} maxLength={128} onChange={event => setReferenceName(event.target.value)} /></label>}
      <div className="mm-forensics-images">{result.images?.map((image, index) => <figure key={index}><figcaption>照片 {index + 1} · 原图人脸位置</figcaption><RegionReview image={file(image.preview)} width={image.width} height={image.height} marks={[]} boxes={image.faces.map(face => ({ box: face.box, label: String(face.face_index + 1) }))} /></figure>)}</div>
      <div className="mm-faces">{result.images?.flatMap((image, input) => image.faces.map(face => <figure key={`${input}:${face.face_index}`}>
        <Image width={112} src={file(face.crop)} alt={`照片${input + 1}人脸${face.face_index + 1}`} />
        <figcaption>照片 {input + 1} · 人脸 {face.face_index + 1}<br /><Text type={face.quality.usable ? 'secondary' : 'warning'}>{face.quality.usable ? '可用于人工比对' : face.quality.note}</Text></figcaption>
        {!readOnly && <Button size="small" loading={referenceBusy} disabled={!face.quality.usable || !referenceName.trim()} onClick={() => onReference(input, face.face_index)}>加入项目参考库</Button>}
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
        { title: '说话人', width: 150, render: (_, row) => <Input readOnly={readOnly} aria-label={`第${row.review_index + 1}段说话人`} value={merged(row).speaker} onChange={event => update(row, 'speaker', event.target.value)} /> },
        { title: '转写与人工校正', render: (_, row) => <Input.TextArea readOnly={readOnly} aria-label={`第${row.review_index + 1}段转写`} autoSize value={merged(row).text} onChange={event => update(row, 'text', event.target.value)} /> },
      ]} />
    </>}
    {item.kind === 'seal' && <>
      <Paragraph>{readOnly ? '以下为固定版本字段与原页来源。' : '候选字段可先转入明确标注未复核的草稿比对；正式字段请人工核准并保存后转入。'}</Paragraph>
      <Select aria-label="印章原页" value={previewPage} onChange={setPreviewPage} options={result.pages?.map(page => ({ value: page.page_number, label: `原件第 ${page.page_number} 页` }))} />
      {result.pages?.filter(page => page.page_number === previewPage).map(page => <RegionReview key={page.page_number} image={file(page.preview)} width={page.width} height={page.height} marks={[]}
        boxes={(result.seals || []).flatMap((seal, index) => seal.page_number === page.page_number && seal.box ? [{ box: seal.box, label: `#${index + 1}` }] : [])} />)}
      <a href={`${API}/workspaces/${workspaceId}/assets/${item.inputs[0].asset_id}/content?revision=${item.inputs[0].asset_revision}`} target="_blank" rel="noreferrer">打开完整原件对照</a>
      <Table<Seal> rowKey="row_id" size="small" pagination={{ pageSize: 10, showSizeChanger: false }} dataSource={sealRows} columns={[
        { title: '原页', dataIndex: 'page_number', width: 70 },
        ...(['text', 'subject', 'number'] as const).map(field => ({ title: { text: '章内文字', subject: '主体名称', number: '编号' }[field], render: (_: unknown, row: Seal) => <Input.TextArea readOnly={readOnly} autoSize aria-label={`印章${row.review_index + 1}${field}`} value={merged(row)[field]} onChange={event => update(row, field, event.target.value)} /> })),
        { title: '自动核对', dataIndex: 'subject_check', render: (value: string) => ({ contains: '包含目标名称', needs_review: '有差异，待人工核对', not_requested: '未指定核对主体' })[value] || value },
      ]} />
      {!readOnly && <><Space wrap><Button loading={draftData.isPending} onClick={() => draftData.mutate()}>候选字段转入草稿比对（未复核）</Button>
        <Button disabled={item.review?.assessment !== 'confirmed'} loading={toData.isPending} onClick={() => toData.mutate()}>将人工核准字段转入 ClearData</Button></Space>
        <Paragraph type="secondary">草稿转换采用当前已保存的算法候选，字段校正请先保存复核。</Paragraph>
        {item.review?.assessment !== 'confirmed' && <Paragraph type="secondary">正式转换等待用户保存“已人工确认”复核；当前候选可继续草稿分析。</Paragraph>}
        {(toData.error || draftData.error) && <Alert type="error" title={(toData.error || draftData.error)?.message} />}</>}
    </>}
    {deep && <><RegionReview image={file(catCoordinatesReady ? displayPreview! : 'preview.png')}
      width={result.width || 1000} height={result.height || 1000} marks={marks} onChange={readOnly ? undefined : setMarks}
      forensic={{ heatmap: classicalOnly ? undefined : file(catCoordinatesReady ? displayHeatmap! : 'heatmap.png'), geometry: catCoordinatesReady ? catGeometry : null,
        heatmapLabel: `${detectorLabel} 定位热图`, suggestions: catCoordinatesReady ? proposed.map((region, index) => ({ box: region.box, note: `建议区域 ${index + 1}` })) : [],
        registered: catCoordinatesReady, warning: catWarning }} />
      {!!proposed.length && <section aria-label="模型建议区域"><Paragraph type="secondary">虚线框为模型建议区域，采纳后可修改边界与说明并保存人工复核。</Paragraph>
        <Space wrap>{proposed.map((region, index) => <Button key={index} disabled={readOnly || !catCoordinatesReady || marks.length >= 200 || marks.some(mark => mark.box.every((value, position) => value === region.box[position]))}
          onClick={() => setMarks(previous => [...previous, { box: region.box, note: `${detectorLabel} 建议区域 ${index + 1}，待人工复核` }])}>采纳建议区域 {index + 1}</Button>)}</Space></section>}
      <Space wrap><Link to={`/apps/forensics?workspace=${encodeURIComponent(workspaceId)}&asset=${encodeURIComponent(item.inputs[0].asset_id)}`}>使用经典取证工具继续核查</Link>
        <Link to={`${routes.aigc_image}?workspace=${encodeURIComponent(workspaceId)}&asset=${encodeURIComponent(item.inputs[0].asset_id)}`}>分析同一原图的整图生成信号</Link></Space></>}
    {item.kind === 'document_forensics' && !!result.classical?.evidence.length && <section aria-label="文档排查线索">
      <Table size="small" pagination={false} rowKey="tool" dataSource={result.classical.evidence} columns={[
        { title: '独立经典检查', dataIndex: 'tool', render: (value: string) => forensicLabels[value] || value },
        { title: '执行情况', dataIndex: 'status', render: (value: string) => ({ completed: '已执行', skipped: '不适用', error: '执行失败' })[value] || value },
        { title: '线索', render: (_, row) => row.map_path ? <a href={file(row.map_path)} target="_blank" rel="noreferrer">查看此工具热图</a> : row.reason || '无热图' },
      ]} />
    </section>}
    <ul className="mm-limitations">{result.limitations.filter(value => item.kind !== 'aigc_image' || !result.aigc_image?.limitations.includes(value)).map((value, index) => <li key={index}>{value}</li>)}</ul>
    {readOnly ? <Space wrap><a href={`${url}/download`}>下载此固定版本</a>{item.review && <Text type="secondary">复核人：{item.review.actor} · {item.review.note}</Text>}</Space> : <div className="mm-review"><Title level={4}>保存人工复核</Title><div className="mm-controls">
      <label>复核人<Input value={actor} maxLength={128} onChange={event => setActor(event.target.value)} /></label>
      <label>复核结论<Select value={assessment} onChange={setAssessment} options={[{ value: 'confirmed', label: '已人工确认所记录结果' }, { value: 'needs_investigation', label: '需要进一步调查' }, { value: 'inconclusive', label: '无法判断' }]} /></label>
    </div><Input.TextArea value={note} maxLength={10000} autoSize={{ minRows: 2 }} placeholder="填写复核依据、修改说明或待查事项" onChange={event => setNote(event.target.value)} />
      {draftError && <Alert type="warning" title={draftError} />}
      <Space wrap><Button type="primary" loading={review.isPending} disabled={!actor.trim() || !note.trim()} onClick={() => review.mutate()}>保存复核与校正</Button>
        <a href={`${url}/download`}>{deep ? '下载已保存版本（报告与热图）' : item.kind === 'aigc_image' ? '下载已保存版本（报告与原始分数）' : '下载已保存版本（含 JSON / CSV / 字幕）'}</a><Button disabled={!item.review} loading={collect.isPending} onClick={() => collect.mutate()}>归集到成果中心</Button>
        <Link to={`/artifacts?workspace=${workspaceId}`}>查看成果</Link></Space>
      {(review.error || collect.error) && <Alert type="error" title={(review.error || collect.error)?.message} />}
      {review.error && <Button onClick={() => { reviewIntent.current = null; localStorage.removeItem(`${draftKey}:request`); review.reset(); refresh() }}>刷新版本并修改复核请求</Button>}
      {collect.data && <Alert type="success" title={`已归集 ${collect.data.files.length} 份固定成果`} description={<Link to={`/audit?workspace=${workspaceId}`}>在审计项目的调查事项中引用此成果</Link>} />}
    </div>}
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
