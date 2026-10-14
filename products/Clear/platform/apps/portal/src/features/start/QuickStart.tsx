import { Alert, Button, Empty, Select, Skeleton, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { useApps, useWorkspaces, useWorkspaceId } from '../../App'
import { applicationDescriptions, brandName } from '../../clear-ui/theme'
import { ApplicationCard } from '../catalog/ApplicationCards'
import { DataApplicationCard } from '../data/DataWorkbench'
import { ForensicApplicationCard } from '../forensics/ForensicWorkbench'

const { Title, Text, Paragraph } = Typography
const toolIds = ['data', 'classification', 'ocr', 'forensics'] as const

export function QuickStart() {
  const [params, setParams] = useSearchParams()
  const projectId = useWorkspaceId(), projects = useWorkspaces(), apps = useApps(projectId)
  const selected = toolIds.find(id => id === params.get('tool'))
  const nativeApp = apps.data?.find(app => app.app_id === selected)
  const selectProject = (id: string) => {
    const next = new URLSearchParams()
    next.set('workspace', id)
    if (selected) next.set('tool', selected)
    setParams(next)
  }
  const selectTool = (id: string) => { const next = new URLSearchParams(params); next.set('tool', id); setParams(next) }
  return <div className="quick-start"><div className="page-heading"><Text className="eyebrow">CLEAR / START</Text>
    <Title level={1}>选择项目，开始处理</Title><Paragraph>选择 Clear 工具，导入资料，直接查看和下载输出。</Paragraph></div>
    <section className="panel quick-step"><div className="section-head"><Title level={3}>1 · 选择项目</Title>
      <Link to={`/projects?create=1${projectId ? `&workspace=${encodeURIComponent(projectId)}` : ''}`}><Button>新建项目</Button></Link></div>
      <Select aria-label="选择处理项目" placeholder="选择已有项目" value={projectId || undefined} loading={projects.isPending}
        onChange={selectProject} options={projects.data?.map(item => ({ value: item.workspace_id, label: item.name }))} />
      {projects.isError && <Alert type="error" title="项目读取失败" action={<Button onClick={() => void projects.refetch()}>重试</Button>} />}
    </section>
    <section className="panel quick-step"><Title level={3}>2 · 选择 Clear 工具</Title>
      <div className="quick-tool-grid">{toolIds.map(id => <button key={id} type="button" className="quick-tool-choice"
        aria-pressed={selected === id} disabled={!projectId} onClick={() => selectTool(id)}>
        <strong>{brandName(id)}</strong><span>{applicationDescriptions[id].purpose}</span>
      </button>)}</div>
    </section>
    <section className="panel quick-step"><div className="section-head"><Title level={3}>3 · 开始处理与输出</Title>
      {projectId && <Link to={`/artifacts?workspace=${encodeURIComponent(projectId)}`}>查看项目输出 →</Link>}</div>
      {!projectId ? <Empty description="先选择或新建项目" /> : !selected ? <Empty description="选择需要使用的 Clear 工具" /> :
        selected === 'data' ? <DataApplicationCard /> : selected === 'forensics' ? <ForensicApplicationCard /> :
          apps.isPending ? <Skeleton active /> : apps.isError ? <Alert type="error" title="工具状态读取失败" action={<Button onClick={() => void apps.refetch()}>重试</Button>} /> :
            nativeApp ? <ApplicationCard key={`${projectId}:${selected}`} app={nativeApp} workspaceId={projectId} projectMode /> : <Alert type="warning" title="工具尚未登记，请查看 Clear 工具目录" />}
    </section>
  </div>
}
