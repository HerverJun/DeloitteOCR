import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Empty, Pagination, Input as SearchInput, Select, Space, Tag, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { ProjectDataOutputs } from '../start/ProjectDataOutputs'
import { ProjectNativeOutputs } from '../start/ProjectNativeOutputs'
import type { ReviewBundle } from '../batches/api'
import type { ForensicBundle, WorkpaperBundle } from '../../../../../packages/contracts/types.generated'
import './artifacts.css'
import { ArtifactFreshness } from './ArtifactFreshness'

const { Title, Text, Paragraph } = Typography
type Input = { role: string; name: string; asset_id: string; revision: number }
type File = { name: string; artifact_id: string; revision: number; size_bytes: number; availability: string }
type OcrItem = { asset_id: string; page_number?: number; revision: number }
type OriginalBundle = { bundle_id: string; kind: string; title: string; status?: string;
  session_id?: string; session_name?: string; export_id?: string;
  project_id?: string; handoff_id?: string; platform_job_id?: string; comparison_id?: string; comparison_revision?: number;
  result_id?: string | null; analysis_kind?: string; review_revision?: number; created_at: string; input_set_id: string; input_set_revision: number;
  inputs: Input[]; items?: OcrItem[]; files: File[] }
type Bundle = OriginalBundle | ReviewBundle | WorkpaperBundle | ForensicBundle
type BundlePage = { items: Bundle[]; total: number; next_offset: number | null }
function isReview(bundle: Bundle): bundle is ReviewBundle { return bundle.kind === 'batch_review' }
function isWorkpaper(bundle: Bundle): bundle is WorkpaperBundle { return bundle.kind === 'audit_workpaper' }
function isForensic(bundle: Bundle): bundle is ForensicBundle { return bundle.kind === 'image_forensics' }

async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { signal })
  if (!response.ok) throw new Error(`成果读取失败（${response.status}）`)
  return response.json() as Promise<T>
}

function size(value: number) {
  return value < 1024 ? `${value} B` : value < 1024 * 1024
    ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`
}

function kindName(bundle: Bundle) {
  if (bundle.kind === 'document_comparison') return '文档主题对比'
  if (bundle.kind === 'manual_analysis') return '多模态人工复核成果'
  if (isWorkpaper(bundle)) return bundle.delivery_kind === 'final' ? '正式复核底稿' : '中间审计底稿'
  if (isReview(bundle)) return bundle.delivery_kind === 'final' ? '正式复核成果' : '中间复核材料'
  if (isForensic(bundle)) return '图像取证报告'
  if (bundle.kind === 'image_forensics') return '图像取证报告'
  if (bundle.kind === 'audit_draft') return '审计底稿草稿'
  if (bundle.kind === 'classification_result') return '分类结果'
  if (bundle.kind === 'ocr_result') return bundle.items?.some(item => item.page_number) ? '文档提取' : 'OCR 识别'
  return bundle.result_id ? '差异结果' : '清洗表'
}

function ReviewDetails({ bundle, workspaceId, base }: { bundle: ReviewBundle; workspaceId: string; base: string }) {
  const scope = encodeURIComponent(workspaceId)
  const link = (id: string) => `/artifacts?workspace=${scope}&bundle=${encodeURIComponent(id)}`
  return <section className="artifact-detail"><div className="artifact-detail-head"><div><Space wrap><Tag>{kindName(bundle)}</Tag>
    <Tag color={bundle.status === 'current' ? 'success' : 'warning'}>{bundle.status === 'current' ? '当前复核版本' : '已过期复核版本'}</Tag></Space><Title level={2}>{bundle.title}</Title>
    <Text type="secondary">生成于 {new Date(bundle.created_at).toLocaleString('zh-CN')} · 固定平台版本 {bundle.batch_revision}</Text></div>
    <Link to={`/batches?workspace=${scope}&batch=${encodeURIComponent(bundle.batch_id)}`}>返回处理批次</Link></div>
    {bundle.status === 'outdated' && <Alert type="warning" title="此复核包已过期" description="后续判断、索引或执行状态发生变化。旧文件保持固定版本，可继续下载并核对前后继。" />}
    {bundle.delivery_kind === 'intermediate' && <Alert type="info" title="中间材料，不代表正式复核完成" description="完整未解决清单和未完成条件保留在 review.json 与 review.md。" />}
    <div className="artifact-meta"><div><Text type="secondary">全量复核覆盖</Text><strong>{bundle.coverage.confirmed || 0} / {bundle.coverage.indexed_items || 0} 已确认</strong></div>
      <div><Text type="secondary">未解决事项</Text><strong>{bundle.unresolved_count} 项</strong></div><div><Text type="secondary">待核实操作</Text><strong>{bundle.pending_operations} 项</strong></div></div>
    <div className="artifact-section"><Title level={3}>版本与来源关系</Title><p>批次 {bundle.batch_id} · 执行状态 {bundle.execution_status} · 索引 {bundle.coverage.index_complete ? '完整' : '未完成'}</p>
      <p>前版：{bundle.predecessor_bundle_id ? <Link to={link(bundle.predecessor_bundle_id)}>查看前版复核包</Link> : '首次复核材料；原始格式文件在下方保留'}</p>
      <p>后继共 {bundle.successor_count} 版{bundle.successor_count > bundle.successor_bundle_ids.length ? `，当前列出 ${bundle.successor_bundle_ids.length} 版` : ''}</p>
      <Space wrap>{bundle.successor_bundle_ids.map((id, index) => <Link key={id} to={link(id)}>查看后继复核包 {index + 1}</Link>)}</Space>
      <details><summary>固定请求与内容身份</summary><p>请求 {bundle.request_id}</p><p>请求摘要 {bundle.payload_hash}</p><p>快照摘要 {bundle.snapshot_hash}</p></details></div>
    <div className="artifact-section"><Title level={3}>固定输入原件</Title><ul>{bundle.inputs.map((input, index) => <li key={`${input.asset_id}-${index}`}><span>{input.role} · {input.asset_id}</span>
      <Text type="secondary">版本 {input.asset_revision} · SHA256 {input.blob_hash}</Text><Link to={`/assets?workspace=${scope}&asset=${encodeURIComponent(input.asset_id)}`}>查看来源</Link></li>)}</ul></div>
    {bundle.unresolved_count > 0 && <div className="artifact-section"><Title level={3}>未解决事项预览</Title><p>全量 {bundle.unresolved_count} 项，页面只展示 {bundle.unresolved_preview.length} 项；完整清单见同包 JSON/Markdown。</p>
      <ul>{bundle.unresolved_preview.map(item => <li key={item.item_id}><span>{item.title}</span><Text type="secondary">{item.disposition}</Text></li>)}</ul></div>}
    <div className="artifact-section"><Title level={3}>成果文件</Title><p>原始输出、平台处置历史、采用关系和来源分别保留。OCR 平台固定结果复核与原生复核资格独立。</p>
      <ul>{bundle.files.map(file => <li key={file.artifact_id}><span>{file.name}</span><Text type="secondary">版本 {file.revision} · {size(file.size_bytes)}</Text>
        {file.availability === 'available' ? <a href={`${base}/${encodeURIComponent(bundle.bundle_id)}/files/${encodeURIComponent(file.name)}`}>下载</a> : <Tag color="error">不可用</Tag>}
        <details><summary>文件身份</summary><p>{file.artifact_id} · SHA256 {file.blob_hash}</p></details></li>)}</ul></div>
  </section>
}

function WorkpaperDetails({ bundle, workspaceId, base }: { bundle: WorkpaperBundle; workspaceId: string; base: string }) {
  const scope = encodeURIComponent(workspaceId)
  const bundleLink = (id: string) => `/artifacts?workspace=${scope}&bundle=${encodeURIComponent(id)}`
  return <section className="artifact-detail">
    <div className="artifact-detail-head"><div><Space wrap><Tag>{kindName(bundle)}</Tag>
      <Tag color={bundle.status === 'current' ? 'success' : 'warning'}>{bundle.status === 'current' ? '当前底稿版本' : '已过期底稿版本'}</Tag></Space>
      <Title level={2}>{bundle.title}</Title><Text type="secondary">生成于 {new Date(bundle.created_at).toLocaleString('zh-CN')} · 固定项目版本 {bundle.project_revision}</Text></div>
      <Link to={`/audit?workspace=${scope}&project=${encodeURIComponent(bundle.project_id)}`}>返回审计项目</Link></div>
    {bundle.status === 'outdated' && <Alert type="warning" title="此底稿已过期" description="项目内容、编复核记录或关联批次已更新。原文件与引用保留固定身份，请核对后继底稿。" />}
    {bundle.delivery_kind === 'intermediate' && <Alert type="info" title="中间底稿，尚未完成正式复核" description="未满足的条件保留在底稿文件中；继续完成说明、结论、编复核或批次复核后生成正式底稿。" />}
    <div className="artifact-meta"><div><Text type="secondary">项目版本</Text><strong>{bundle.project_revision}</strong></div>
      <div><Text type="secondary">关联复核成果</Text><strong>{bundle.review_refs.length} 包</strong></div>
      <div><Text type="secondary">未解决条件</Text><strong>{bundle.unresolved_count} 项</strong></div></div>
    <div className="artifact-section"><Title level={3}>版本与生成来源</Title>
      <p>生成声明：{bundle.actor.display_name}（本机声明） · 项目 {bundle.project_id}</p>
      <p>业务编制与复核记录保留在固定底稿文件中。</p>
      <p>前版：{bundle.predecessor_workpaper_id ? <Link to={bundleLink(bundle.predecessor_workpaper_id)}>查看前版底稿</Link> : '首次底稿'}</p>
      <p>后继共 {bundle.successor_count} 版{bundle.successor_count > bundle.successor_workpaper_ids.length ? `，当前列出 ${bundle.successor_workpaper_ids.length} 版` : ''}</p>
      <Space wrap>{bundle.successor_workpaper_ids.map((id, index) => <Link key={id} to={bundleLink(id)}>查看后继底稿 {index + 1}</Link>)}</Space>
      <details><summary>固定请求与内容身份</summary><p>请求 {bundle.request_id}</p><p>请求摘要 {bundle.payload_hash}</p><p>快照摘要 {bundle.snapshot_hash}</p></details></div>
    <div className="artifact-section"><Title level={3}>复核成果与明细引用</Title>
      {!!bundle.forensic_refs?.length && <ul>{bundle.forensic_refs.map(ref => <li key={ref.evidence_id}>
        <Link to={`/apps/forensics?workspace=${scope}&inspection=${encodeURIComponent(ref.inspection_id)}`}>查看图像取证报告</Link>
        <Text type="secondary">固定人工审阅版本 {ref.inspection_revision} · 报告 SHA256 {ref.report_hash}</Text></li>)}</ul>}
      {bundle.review_refs.length ? <ul>{bundle.review_refs.map(ref => <li key={ref.bundle_id}>
        <Link to={bundleLink(ref.bundle_id)}>查看复核成果 · 批次 {ref.batch_id}</Link>
        <Text type="secondary">固定批次版本 {ref.batch_revision}</Text>
        {ref.results_xlsx.availability === 'available' ? <a href={`${base}/${encodeURIComponent(ref.bundle_id)}/files/results.xlsx`}>下载所引用的 XLSX 明细</a> : <Tag color="error">引用明细不可用</Tag>}
        <details><summary>引用身份</summary><p>复核快照 {ref.snapshot_hash}</p><p>{ref.results_xlsx.artifact_id} · 文件版本 {ref.results_xlsx.revision} · SHA256 {ref.results_xlsx.blob_hash}</p></details>
      </li>)}</ul> : <Text type="secondary">尚未关联明确复核成果；详见中间底稿的未解决条件。</Text>}</div>
    {!!bundle.unresolved_reasons.length && <div className="artifact-section"><Title level={3}>未解决条件</Title>
      <p>全量 {bundle.unresolved_count} 项，页面列出 {bundle.unresolved_reasons.length} 条原因；完整条件保留在底稿文件。</p>
      <ul>{bundle.unresolved_reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul></div>}
    <div className="artifact-section"><Title level={3}>底稿文件</Title>
      <p>Markdown 用于阅读；JSON 保留固定项目、程序、调查、编复核与证据引用。</p>
      <ul>{bundle.files.map(file => <li key={file.artifact_id}><span>{file.name}</span><Text type="secondary">文件版本 {file.revision} · {size(file.size_bytes)}</Text>
        {file.availability === 'available' ? <a href={`${base}/${encodeURIComponent(bundle.bundle_id)}/files/${encodeURIComponent(file.name)}`}>下载</a> : <Tag color="error">不可用</Tag>}
        <details><summary>文件身份</summary><p>{file.artifact_id} · SHA256 {file.blob_hash}</p></details></li>)}</ul></div>
  </section>
}

function BundleDetails({ bundle, workspaceId, base }: { bundle: Bundle; workspaceId: string; base: string }) {
  if (isWorkpaper(bundle)) return <WorkpaperDetails bundle={bundle} workspaceId={workspaceId} base={base} />
  if (isReview(bundle)) return <ReviewDetails bundle={bundle} workspaceId={workspaceId} base={base} />
  if (isForensic(bundle)) return <section className="artifact-detail"><div className="artifact-detail-head"><div><Tag>图像取证报告</Tag><Title level={2}>{bundle.title}</Title></div>
    <Link to={`/apps/forensics?workspace=${encodeURIComponent(workspaceId)}&inspection=${bundle.inspection_id}`}>返回图像审阅</Link></div>
    <Paragraph>报告和热图保持固定身份；人工说明的版本保存在审计调查证据与底稿中。</Paragraph>
    <a href={`/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/forensics/${bundle.inspection_id}/download`}>下载完整报告（含图片）</a>
    <div className="artifact-section"><Title level={3}>输入原件</Title>{bundle.inputs.map(input => <p key={input.asset_id}><Link to={`/assets?workspace=${workspaceId}&asset=${input.asset_id}`}>{input.name}</Link> · 来源版本 {input.revision}</p>)}</div>
    <div className="artifact-section"><Title level={3}>取证成果文件</Title><ul>{bundle.files.map(file => <li key={file.artifact_id}><span>{file.name}</span><Text type="secondary">{size(file.size_bytes)}</Text><a href={`${base}/${bundle.bundle_id}/files/${encodeURIComponent(file.name)}`}>下载</a></li>)}</ul></div>
  </section>
  const scope = encodeURIComponent(workspaceId)
  const classification = bundle.kind === 'classification_result'
  const ocr = bundle.kind === 'ocr_result'
  if (bundle.kind === 'document_comparison') {
    return <section className="artifact-detail"><div className="artifact-detail-head"><div><Tag>文档主题对比</Tag><Title level={2}>{bundle.title}</Title></div>
      <Link to={`/apps/comparisons?workspace=${scope}&comparison=${encodeURIComponent(bundle.comparison_id || '')}&comparison_revision=${bundle.comparison_revision || 1}`}>返回文档主题对比</Link></div>
      <Paragraph>固定修订 v{bundle.comparison_revision} · {new Date(bundle.created_at).toLocaleString('zh-CN')}</Paragraph>
      {bundle.status === 'outdated' && <Alert type="warning" title="此报告已有后续修订" description="当前下载保留此修订的材料、证据和报告。" />}
      <Title level={3}>输入原件</Title><ul>{bundle.inputs.map((input, index) => <li key={`${input.asset_id}-${index}`}><Link to={`/assets?workspace=${scope}&asset=${encodeURIComponent(input.asset_id)}`}>{input.name}</Link> · 来源版本 {input.revision}</li>)}</ul>
      <Title level={3}>固定成果文件</Title><ul>{bundle.files.map(file => <li key={file.artifact_id}><span>{file.name} · {size(file.size_bytes)} </span>
        <a href={`${base}/${bundle.bundle_id}/files/${encodeURIComponent(file.name)}`}>下载固定成果</a></li>)}</ul></section>
  }
  if (bundle.kind === 'manual_analysis') {
    const paths: Record<string, string> = { face: '/apps/face', audio: '/apps/audio', seal: '/apps/ocr/seals', catnet: '/apps/forensics/deep', document_forensics: '/apps/forensics/document', aigc_image: '/apps/forensics/aigc' }
    const route = paths[bundle.analysis_kind || ''] || '/apps'
    return <section className="artifact-detail"><div className="artifact-detail-head"><Title level={2}>{bundle.title}</Title>
      <Link to={`${route}?workspace=${scope}&analysis=${encodeURIComponent(bundle.platform_job_id || '')}`}>返回分析与复核</Link></div>
      <Paragraph>固定人工复核版本 {bundle.review_revision} · {new Date(bundle.created_at).toLocaleString('zh-CN')}</Paragraph>
      <Title level={3}>输入原件</Title>{bundle.inputs.map(input => <p key={input.asset_id}><Link to={`/assets?workspace=${scope}&asset=${input.asset_id}`}>{input.name}</Link> · 来源版本 {input.revision}</p>)}
      <Title level={3}>成果文件</Title><ul>{bundle.files.map(file => <li key={file.artifact_id}><span>{file.name} · {size(file.size_bytes)} </span>
        <a href={`${base}/${bundle.bundle_id}/files/${encodeURIComponent(file.name)}`}>下载固定成果</a></li>)}</ul></section>
  }
  return <section className="artifact-detail">
    <div className="artifact-detail-head"><div><Tag>{kindName(bundle)}</Tag>
      <Title level={2}>{bundle.title}</Title><Text type="secondary">生成于 {new Date(bundle.created_at).toLocaleString('zh-CN')}</Text></div>
      {classification
        ? <Space><Link to={`/apps/classification/workflow?workspace=${scope}&set=${encodeURIComponent(bundle.input_set_id)}&project=${encodeURIComponent(bundle.project_id || '')}&handoff=${encodeURIComponent(bundle.handoff_id || '')}`}>返回分类项目</Link>
            <Link to={`/jobs?workspace=${scope}`}>查看任务</Link></Space>
        : ocr ? <Space><Link to={`/apps/ocr/workflow?workspace=${scope}&set=${encodeURIComponent(bundle.input_set_id)}&project=${encodeURIComponent(bundle.project_id || '')}&handoff=${encodeURIComponent(bundle.handoff_id || '')}`}>返回 OCR 项目</Link>
            <Link to={`/jobs?workspace=${scope}`}>查看任务</Link></Space>
        : bundle.kind === 'document_comparison' ? <Link to={`/apps/comparisons?workspace=${scope}&comparison=${encodeURIComponent(bundle.comparison_id || bundle.platform_job_id || bundle.result_id || '')}${bundle.comparison_revision ? `&comparison_revision=${bundle.comparison_revision}` : ''}`}>返回文档主题对比</Link>
          : <Link to={`/apps/data?workspace=${scope}&session=${encodeURIComponent(bundle.session_id || '')}`}>返回数据作业</Link>}</div>
    <div className="artifact-meta"><div><Text type="secondary">输入集合</Text><strong>版本 {bundle.input_set_revision}</strong></div>
      <div><Text type="secondary">成果文件</Text><strong>{bundle.files.length} 件</strong></div>
      <div><Text type="secondary">作业类型</Text><strong>{bundle.kind === 'document_comparison' ? '文档主题对比' : bundle.kind === 'image_forensics' ? '图像取证' : classification ? '图像分类' : ocr ? kindName(bundle) : bundle.result_id ? '表格比对' : '表格清洗'}</strong></div></div>
    <div className="artifact-section"><Title level={3}>输入资料</Title>
      <ul>{bundle.inputs.map((input, index) => <li key={`${input.role}-${input.asset_id}-${index}`}><span>{input.name}</span>
        <Text type="secondary">版本 {input.revision}</Text>
        <Link to={`/assets?workspace=${scope}&asset=${encodeURIComponent(input.asset_id)}`}>查看来源</Link></li>)}</ul></div>
    {ocr && !!bundle.items?.length && <div className="artifact-section"><Title level={3}>识别结果</Title>
      <ul>{bundle.items.map((item, index) => <li key={`${item.asset_id}-${item.page_number || index}`}>
        <span>{item.page_number ? `第 ${item.page_number} 页` : bundle.inputs.find(input => input.asset_id === item.asset_id)?.name || `图片 ${index + 1}`}</span>
        <Text type="secondary">结果版本 {item.revision}</Text>
        <Link to={`/assets?workspace=${scope}&asset=${encodeURIComponent(item.asset_id)}`}>查看原件</Link></li>)}</ul></div>}
    <div className="artifact-section"><Title level={3}>成果文件</Title>
      <ul>{bundle.files.map(file => <li key={file.artifact_id}><span>{file.name}</span>
        <Text type="secondary">版本 {file.revision} · {size(file.size_bytes)}</Text>
        {file.availability === 'available'
          ? <a href={`${base}/${encodeURIComponent(bundle.bundle_id)}/files/${encodeURIComponent(file.name)}`}>下载</a>
          : <Tag color="error">不可用</Tag>}</li>)}</ul></div>
  </section>
}

export function ArtifactCenter() {
  const [params] = useSearchParams()
  const workspaceId = params.get('workspace')
  const bundleId = params.get('bundle')
  return <WorkspaceArtifacts key={workspaceId || 'none'} workspaceId={workspaceId} bundleId={bundleId} />
}

function WorkspaceArtifacts({ workspaceId, bundleId }: { workspaceId: string | null; bundleId: string | null }) {
  const [page, setPage] = useState(1)
  const [keyword, setKeyword] = useState(''), [kind, setKind] = useState<string>(), [nativeOutputs, setNativeOutputs] = useState(false)
  const base = workspaceId ? `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/artifacts` : ''
  const list = useQuery({ queryKey: ['artifact-bundles', workspaceId, page], enabled: !!workspaceId && !bundleId,
    queryFn: ({ signal }) => read<BundlePage>(`${base}?offset=${(page - 1) * 20}&limit=20`, signal) })
  const detail = useQuery({ queryKey: ['artifact-bundle', workspaceId, bundleId], enabled: !!workspaceId && !!bundleId,
    queryFn: ({ signal }) => read<Bundle>(`${base}/${encodeURIComponent(bundleId || '')}`, signal) })
  return <div className="artifact-center"><div className="artifact-heading"><Text className="eyebrow">OUTPUTS / WORKSPACE</Text>
    <Title level={1}>成果</Title><Paragraph>查看当前项目已生成的文件，直接下载或继续处理。</Paragraph></div>
    {!workspaceId ? <Empty description="先选择项目" /> : bundleId ? <>
      <div className="artifact-back"><Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}`}>返回全部成果</Link></div>
      {detail.isPending && <Text>正在读取成果…</Text>}
      {detail.isError && <Alert type="error" title={detail.error.message}
        action={<Button onClick={() => void detail.refetch()}>重试</Button>} />}
      {detail.data && <><Link to={`/agent?workspace=${encodeURIComponent(workspaceId)}&bundle=${encodeURIComponent(detail.data.bundle_id)}`}><Button>交给 Agent 继续处理</Button></Link>
        <ArtifactFreshness workspaceId={workspaceId} bundleId={detail.data.bundle_id} inputSetId={'input_set_id' in detail.data ? detail.data.input_set_id : undefined} projectId={'project_id' in detail.data ? detail.data.project_id : undefined} handoffId={'handoff_id' in detail.data ? detail.data.handoff_id : undefined} />
        <BundleDetails bundle={detail.data} workspaceId={workspaceId} base={base} /></>}
    </> : <>
      <div className="artifact-toolbar"><SearchInput.Search aria-label="搜索本页成果" placeholder="搜索本页成果名称或文件" value={keyword} allowClear onChange={event => setKeyword(event.target.value)} /><Select aria-label="成果类型" placeholder="全部类型" allowClear value={kind} onChange={setKind} options={[...new Map((list.data?.items || []).map(item => [item.kind, {value: item.kind, label: kindName(item)}])).values()]} /></div>
      <details className="artifact-native-outputs" onToggle={event => setNativeOutputs(event.currentTarget.open)}><summary>工具内输出与导出</summary>{nativeOutputs && <><ProjectDataOutputs key={workspaceId} workspaceId={workspaceId} /><ProjectNativeOutputs workspaceId={workspaceId} /></>}</details>
      {list.isError && <Alert type="error" title={list.error.message}
        action={<Button onClick={() => void list.refetch()}>重试</Button>} />}
      {list.isPending && <Text>正在读取成果…</Text>}
      {list.data?.total === 0 && <Text type="secondary">暂无其他工具输出</Text>}
      <div className="artifact-list">{list.data?.items.filter(bundle => (!kind || bundle.kind === kind) && `${bundle.title} ${bundle.files.map(file => file.name).join(' ')}`.toLocaleLowerCase().includes(keyword.toLocaleLowerCase())).map(bundle => <article key={bundle.bundle_id}>
        <div><Tag>{kindName(bundle)}</Tag><Title level={3}>{bundle.title}</Title>
          {isReview(bundle) && <Tag color={bundle.status === 'current' ? 'success' : 'warning'}>{bundle.status === 'current' ? '当前复核版本' : '已过期复核版本'} · 平台版本 {bundle.batch_revision}</Tag>}
          <Text type="secondary">{new Date(bundle.created_at).toLocaleString('zh-CN')}</Text></div>
        <details><summary>来源与版本</summary><div className="artifact-list-input"><Text type="secondary">{isWorkpaper(bundle) ? '审计项目' : '输入'}</Text><span>{isWorkpaper(bundle) ? `${bundle.project_id} · 项目版本 ${bundle.project_revision} · ${bundle.status === 'current' ? '当前底稿' : '已过期底稿'}` : isReview(bundle) ? bundle.inputs.map(item => `${item.role} · ${item.asset_id} · 版本 ${item.asset_revision}`).join(' · ') : bundle.inputs.map(item => item.name).join(' · ')}</span></div></details>
        <Space wrap><Text type="secondary">{bundle.files.length} 件文件</Text>
          {isForensic(bundle) && <a href={`/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/forensics/${bundle.inspection_id}/download`}>下载完整报告（含图片）</a>}
          {bundle.files.filter(file => file.availability === 'available' && /\.(xlsx|csv|parquet|pdf|html|md|txt|zip)$/i.test(file.name)).slice(0, 6).map(file =>
            <a key={file.artifact_id} href={`${base}/${encodeURIComponent(bundle.bundle_id)}/files/${encodeURIComponent(file.name)}`} download>{file.name}</a>)}
          <Link to={`/artifacts?workspace=${encodeURIComponent(workspaceId)}&bundle=${encodeURIComponent(bundle.bundle_id)}`}>全部文件与详情</Link></Space>
        <Link to={`/agent?workspace=${encodeURIComponent(workspaceId)}&bundle=${encodeURIComponent(bundle.bundle_id)}`}>交给 Agent</Link>
      </article>)}</div>
      {list.data && !list.data.items.some(bundle => (!kind || bundle.kind === kind) && (bundle.title + ' ' + bundle.files.map(file => file.name).join(' ')).toLocaleLowerCase().includes(keyword.toLocaleLowerCase())) && <Empty description="本页没有匹配的成果" />}
      {(list.data?.total || 0) > 20 && <Pagination current={page} pageSize={20} total={list.data?.total}
        onChange={setPage} showSizeChanger={false} />}
    </>}
  </div>
}
