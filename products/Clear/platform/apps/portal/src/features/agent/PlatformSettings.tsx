import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Descriptions, Space, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { useWorkspaceId } from '../../App'
import { agentRequest, type Controller } from './api'
import { ControllerSettings } from './ControllerSettings'
import { SkillsPanel } from './SkillsPanel'
import { IntegrationSettings } from './IntegrationSettings'
import { navigationHref } from '../../clear-ui/navigation'
import './agent.css'

export function PlatformSettings() {
  const [open, setOpen] = useState(false), [skills, setSkills] = useState(false)
  const [integrations, setIntegrations] = useState(false)
  const [params] = useSearchParams()
  const workspace = useWorkspaceId()
  const health = useQuery({ queryKey: ['agent-health'], queryFn: () => agentRequest<{ enabled: boolean; reason: string }>('/agent/health') })
  const connection = useQuery({ queryKey: ['agent-connection'], enabled: health.data?.enabled,
    queryFn: () => agentRequest<Controller>('/agent/connection') })
  const capabilities = useQuery({ queryKey: ['audit-model-capabilities', workspace], enabled: !!workspace && health.data?.enabled,
    queryFn: () => agentRequest<{ tools: { name: string; availability: { state: string; reason?: string } }[] }>(`/workspaces/${workspace}/agent/capabilities`) })
  const scoped = (path: string) => navigationHref(path, params.toString(), '', '/settings')
  return <div className="audit-settings"><div className="page-heading"><span className="eyebrow">PLATFORM SETTINGS</span><Typography.Title level={1}>模型与平台设置</Typography.Title></div>
    <Typography.Paragraph type="secondary">连接负责对话和安排任务的主控模型，专业工具继续使用已有的本地运行环境。</Typography.Paragraph>
    {!health.data?.enabled && health.data && <Alert type="warning" title="智能审计当前未启用" description={health.data.reason} />}
    {(connection.error || health.error) && <Alert type="error" title={(connection.error || health.error)?.message} />}
    <section className="audit-settings-section"><Typography.Title level={3}>审计主控</Typography.Title>
      <Descriptions column={1} items={[
        { key: 'status', label: '连接状态', children: connection.data?.available ? '已连接' : '待配置' },
        { key: 'model', label: '主控模型', children: connection.data?.model || '尚未选择' },
        { key: 'endpoint', label: '服务地址', children: connection.data?.base_url || '尚未配置' },
      ]} />
      <Button type="primary" disabled={!health.data?.enabled} onClick={() => setOpen(true)}>配置或更换主控</Button>
    </section>
    <section className="audit-settings-section"><Typography.Title level={3}>专业能力</Typography.Title>
      <Typography.Paragraph>ClearOCR 识别资料，ClearData 处理表格，ClearClassify 筛选图像，ClearForensics 检查取证线索。</Typography.Paragraph>
      {capabilities.data && <Space wrap>{(['ocr_run', 'data_compare', 'classification_infer', 'forensics_inspect'] as const).map(name => {
        const item = capabilities.data.tools.find(tool => tool.name === name)
        return <span key={name} className="audit-capability-status" title={item?.availability.reason} data-state={item?.availability.state || 'unknown'}>{({ ocr_run: 'OCR', data_compare: '数据处理', classification_infer: '分类', forensics_inspect: '取证' })[name]} · {item?.availability.state === 'ready' ? '可用' : item?.availability.state === 'preparable' ? '可按需启动' : item?.availability.state === 'unavailable' ? '不可用' : item?.availability.state === 'disabled' ? '未启用' : '未确认'}</span>
      })}</Space>}
      <div className="audit-settings-links"><Link to={scoped('/apps')}>打开手动工具</Link><Link to={scoped('/agent')}>返回智能审计</Link></div>
    </section>
    <section className="audit-settings-section"><Typography.Title level={3}>Agent 技能</Typography.Title><Typography.Paragraph>Agent 按目标选择已登记的方法。可以查看技能目录，在已有任务中调整采用的技能。</Typography.Paragraph><Button disabled={!health.data?.enabled} onClick={() => setSkills(true)}>浏览技能目录</Button></section>
    <ControllerSettings open={open} close={() => setOpen(false)} />
    <section className="audit-settings-section"><Typography.Title level={3}>Agent 通用能力</Typography.Title><Typography.Paragraph>支持文件与代码处理、运行脚本、后台任务和网页读取；MCP 连接与子助手可按需配置。</Typography.Paragraph><Button disabled={!health.data?.enabled} onClick={() => setIntegrations(true)}>配置扩展</Button></section>
    <IntegrationSettings open={integrations} close={() => setIntegrations(false)} />
    <SkillsPanel open={skills} close={() => setSkills(false)} workspace={workspace} sessionId={null} />
  </div>
}
