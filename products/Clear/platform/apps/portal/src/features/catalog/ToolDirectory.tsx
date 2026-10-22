import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Empty, Input, Skeleton, Typography, type InputRef } from 'antd'
import ArrowRightOutlined from '@ant-design/icons/es/icons/ArrowRightOutlined'
import BookOutlined from '@ant-design/icons/es/icons/BookOutlined'
import FileSearchOutlined from '@ant-design/icons/es/icons/FileSearchOutlined'
import SearchOutlined from '@ant-design/icons/es/icons/SearchOutlined'
import SafetyCertificateOutlined from '@ant-design/icons/es/icons/SafetyCertificateOutlined'
import FilterOutlined from '@ant-design/icons/es/icons/FilterOutlined'
import { Link, useSearchParams } from 'react-router-dom'
import { useApps, useWorkspaceId, useWorkspaces } from '../../App'
import { ApplicationCard, RecentWorks } from './ApplicationCards'
import { DataApplicationCard } from '../data/DataWorkbench'
import { ForensicApplicationCard } from '../forensics/ForensicWorkbench'
import { MultimodalApplicationCard } from '../multimodal/MultimodalWorkbench'
import { ComparisonApplicationCard } from '../comparisons/ComparisonApplicationCard'
import { ReferenceLinks } from '../agent/TaskPanel'
import { agentRequest, type AuditTask } from '../agent/api'
import './directory.css'

type Category = 'all' | 'document' | 'data' | 'image' | 'audio'
const filters: { id: Category; label: string }[] = [
  { id: 'all', label: '全部工具' }, { id: 'document', label: '文档识别' },
  { id: 'data', label: '数据处理' }, { id: 'image', label: '图像分析' }, { id: 'audio', label: '音频处理' },
]
const metadata: Record<string, { category: Category; label: string; keywords: string; formats: string }> = {
  ocr: { category: 'document', label: '文档识别', keywords: 'ClearOCR OCR 文字 文档 识别 表格 校对 PDF 扫描 提取 发票', formats: 'PDF · 图片 → 文字 / 表格' },
  comparisons: { category: 'document', label: '文档对比', keywords: 'PDF PPTX 图片 幻灯片 文档 主题 对比 比较 进度 多模态 HTML 报告', formats: 'PDF · PPTX · 图片 → 主题对照 / 报告' },
  data: { category: 'data', label: '数据处理', keywords: 'ClearData 数据 清洗 比对 核对 Excel CSV 差异 表格', formats: 'Excel · CSV → 差异 / 清洗结果' },
  classification: { category: 'image', label: '图像分析', keywords: 'ClearClassify 分类 图像 归类 训练 标注 复核', formats: '图片 → 分类 / 复核结果' },
  forensics: { category: 'image', label: '图像分析', keywords: 'ClearForensics 取证 图片 图像 篡改 EXIF 热图 报告', formats: '图片 → 热图 / 取证报告' },
  face: { category: 'image', label: '图像分析', keywords: '人脸 比对 图片 照片 参考库 相似度 检索', formats: '照片 → 人脸 / 比对结果' },
  audio: { category: 'audio', label: '音频处理', keywords: '音频 分析 录音 中文 转写 语音 说话人 波形', formats: '录音 → 转写 / 定位片段' },
}

export function ToolDirectory() {
  const workspaceId = useWorkspaceId(), apps = useApps(workspaceId), workspaces = useWorkspaces()
  const [params] = useSearchParams(), taskId = params.get('task_return')
  const [category, setCategory] = useState<Category>('all'), [search, setSearch] = useState('')
  const input = useRef<InputRef>(null)
  const task = useQuery({ queryKey: ['audit-task', taskId], enabled: !!taskId && !!workspaceId,
    queryFn: () => agentRequest<AuditTask>(`/agent/sessions/${encodeURIComponent(taskId!)}/task`) })
  const scope = params.size ? `?${params}` : ''
  const selected = workspaces.data?.find(item => item.workspace_id === workspaceId)
  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault(); input.current?.focus()
      }
    }
    window.addEventListener('keydown', focusSearch)
    return () => window.removeEventListener('keydown', focusSearch)
  }, [])
  const native = (id: string) => apps.data?.find(app => app.app_id === id)
  const entries = [
    ...(native('ocr') ? [{ id: 'ocr', card: <ApplicationCard app={native('ocr')!} workspaceId={workspaceId} projectMode={!!workspaceId} /> }] : []),
    { id: 'data', card: <DataApplicationCard /> },
    { id: 'comparisons', card: <ComparisonApplicationCard /> },
    ...(native('classification') ? [{ id: 'classification', card: <ApplicationCard app={native('classification')!} workspaceId={workspaceId} projectMode={!!workspaceId} /> }] : []),
    { id: 'forensics', card: <ForensicApplicationCard /> },
    { id: 'face', card: <MultimodalApplicationCard kind="face" /> },
    { id: 'audio', card: <MultimodalApplicationCard kind="audio" /> },
    ...(apps.data?.filter(app => !['ocr', 'classification', 'data', 'forensics', 'face', 'audio', 'comparisons'].includes(app.app_id)).map(app => ({
      id: app.app_id, card: <ApplicationCard app={app} workspaceId={workspaceId} projectMode={!!workspaceId} />,
    })) || []),
  ]
  const query = search.trim().toLocaleLowerCase()
  const visible = entries.filter(entry => (category === 'all' || metadata[entry.id]?.category === category)
    && (!query || `${entry.id} ${metadata[entry.id]?.keywords || ''}`.toLocaleLowerCase().includes(query)))
  return <div className="manual-directory">
    <header className="directory-heading">
      <div><div className="directory-kicker"><span />CLEAR WORKSPACE</div>
        <Typography.Title level={1}>手动工具<span className="directory-title-dot" aria-hidden="true">.</span></Typography.Title>
        <Typography.Paragraph>从识别到复核，让每一份资料都有据可查。</Typography.Paragraph></div>
      <Link className="directory-guide" to={`/help${scope}`}><BookOutlined /> 使用指南 <ArrowRightOutlined /></Link>
    </header>

    <section className="directory-context" aria-label="当前工作范围">
      <div className="directory-context-icon"><FileSearchOutlined /></div>
      <div><strong>{selected ? selected.name : '选择工具，开始处理资料'}</strong>
        <p>{selected ? '工具会沿用当前项目，处理结果可归集到项目成果。' : 'OCR 和数据工具可直接使用；需要项目的工具会引导你选择或创建。'}</p></div>
      <Link to={`/projects${scope}`}>{selected ? '查看项目' : '管理审计项目'} <ArrowRightOutlined /></Link>
    </section>
    {task.data?.workspace_id === workspaceId && !!(task.data.result_refs?.length || task.data.initial_refs?.length || task.data.job_refs?.length) &&
      <section className="panel"><Typography.Title level={3}>当前任务已有结果</Typography.Title>
        <ReferenceLinks taskId={taskId || undefined} refs={[...task.data.result_refs, ...task.data.initial_refs, ...task.data.job_refs]} /></section>}

    <section className="directory-tools" aria-label="工具检索">
      <div className="directory-toolbar">
        <div className="directory-filters" role="group" aria-label="按工具类型筛选">
          {filters.map(filter => <button key={filter.id} type="button" aria-pressed={category === filter.id}
            onClick={() => setCategory(filter.id)}>{filter.label}{filter.id === 'all' && !apps.isPending && <span>{entries.length}</span>}</button>)}
        </div>
        <Input ref={input} className="directory-search" prefix={<SearchOutlined />} value={search} allowClear
          onChange={event => setSearch(event.target.value)} placeholder="搜索工具或用途" aria-label="搜索工具或用途"
          suffix={!search && <kbd>Ctrl K</kbd>} />
      </div>
      <div className="directory-result-caption" role="status" aria-live="polite">
        <span>{apps.isPending ? '正在读取工具目录…' : `${visible.length} 个工具${category !== 'all' || query ? '符合筛选' : '，按需打开'}`}</span>
        <span>原件对照 · 人工确认 · 成果留存</span>
      </div>
      {apps.isError && <Alert className="directory-load-error" type="error" showIcon title="暂时无法取得服务数据"
        description="其他工具仍可使用。重试读取 OCR 和分类工具。" action={<Button onClick={() => void apps.refetch()}>重试</Button>} />}
      <div className="app-grid" aria-label="Clear 应用目录">
        {visible.map(entry => <article className="catalog-tool" key={entry.id} data-tool={entry.id}>
          <div className="catalog-tool-label"><span>{metadata[entry.id]?.label || '扩展工具'}</span><span>{metadata[entry.id]?.formats}</span></div>
          {entry.card}
        </article>)}
        {apps.isPending && category === 'all' && !query && <div className="catalog-tool catalog-loading" role="status" aria-label="正在读取原生工具"><Skeleton active paragraph={{ rows: 4 }} /></div>}
      </div>
      {!visible.length && !apps.isPending && <div className="directory-empty"><Empty image={Empty.PRESENTED_IMAGE_SIMPLE}
        description={query ? `没有找到与“${search.trim()}”匹配的工具` : '这个分类下没有工具'} />
        <Button icon={<FilterOutlined />} onClick={() => { setSearch(''); setCategory('all'); input.current?.focus() }}>显示全部工具</Button></div>}
    </section>

    <section className="directory-extensions" aria-label="专项扩展">
      <div className="extension-intro"><span className="directory-section-label">专项扩展</span><p>深入检查文档与图像</p></div>
      <Link to={`/apps/ocr/seals${scope}`}><span className="extension-icon"><SafetyCertificateOutlined /></span>
        <span><strong>印章识别</strong><small>定位印章、核对文字与主体</small></span><ArrowRightOutlined /></Link>
      <Link to={`/apps/forensics/deep${scope}`}><span className="extension-icon"><SearchOutlined /></span>
        <span><strong>SAFIRE 深度定位</strong><small>JPEG、PNG 可疑区域辅助排查</small></span><ArrowRightOutlined /></Link>
      <Link to={`/apps/forensics/document${scope}`}><span className="extension-icon"><FileSearchOutlined /></span>
        <span><strong>文档篡改排查</strong><small>改字定位、原件对照与复核</small></span><ArrowRightOutlined /></Link>
    </section>
    <RecentWorks workspaceId={workspaceId} />
    <footer className="directory-footnote"><span>Clear 审计工作台</span><span>分析结果经人工复核后形成审计成果</span></footer>
  </div>
}
