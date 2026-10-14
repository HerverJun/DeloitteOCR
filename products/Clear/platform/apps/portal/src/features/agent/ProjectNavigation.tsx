import { Link, useLocation } from 'react-router-dom'

const sections = [{ path: '/projects', name: '项目概况' }, { path: '/assets', name: '资料' },
  { path: '/findings', name: '发现' }, { path: '/artifacts', name: '成果' }]

export function ProjectNavigation() {
  const location = useLocation()
  const href = (path: string) => `${path}${location.search}${location.hash}`
  return <div className="project-navigation">
    <nav aria-label="项目内容">{sections.map(section => <Link key={section.path} to={href(section.path)}
      aria-current={location.pathname === section.path || section.path === '/projects' && location.pathname === '/workspaces' ? 'page' : undefined}>{section.name}</Link>)}</nav>
    <details><summary>正式档案与运行详情</summary><div className="project-related-links">
      <Link to={href('/audit')}>正式档案与人工记录</Link><Link to={href('/todos')}>待办事项</Link>
      <Link to={href('/batches')}>模板批次</Link><Link to={href('/jobs')}>运行任务</Link>
    </div></details>
  </div>
}
