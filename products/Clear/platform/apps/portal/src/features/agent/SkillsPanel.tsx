import { useEffect, useState } from 'react'
import { Alert, Button, Drawer, Empty, Input, Select, Space, Spin, Tag, Typography } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { agentRequest, agentWrite, requestId, type AgentSkill, type AgentSkillResourceResponse, type AgentSkillsPage } from './api'

const versionLabel = (value?: string | null) => value ? value.slice(0, 12) : '未提供'
const sourceLabel = (value: string) => value.startsWith('platform:') ? `平台登记 · ${value.slice(9)}` : value.startsWith('workspace:') ? `项目登记 · ${value.slice(10)}` : value
const resourceLabels: Record<string, string> = { reference: '参考资料', template: '模板', script: '脚本资源', resource: '资源', skill: '技能正文', instructions: '技能正文' }
const resourceLabel = (kind: string) => resourceLabels[kind] || '资源'

export function SkillsPanel({ open, close, workspace, sessionId, running = false }: {
  open: boolean; close: () => void; workspace: string | null; sessionId: string | null; running?: boolean
}) {
  const [keyword, setKeyword] = useState(''), [offset, setOffset] = useState(0), [error, setError] = useState('')
  const [pending, setPending] = useState<{ name: string; action: 'activate' | 'deactivate'; version?: string }[]>([])
  const [reading, setReading] = useState<{ name: string; version: string; path: string; offset: number } | null>(null)
  const client = useQueryClient()
  const query = new URLSearchParams({ offset: String(offset), limit: '20', keyword })
  if (workspace) query.set('workspace_id', workspace)
  if (sessionId) query.set('session_id', sessionId)
  const listing = useQuery({ queryKey: ['agent-skills', workspace, sessionId, keyword, offset], enabled: open,
    queryFn: () => agentRequest<AgentSkillsPage>(`/agent/skills?${query}`), refetchInterval: open && sessionId ? 4000 : false })
  const resource = useQuery({ queryKey: ['agent-skill-resource', sessionId, reading], enabled: open && !!sessionId && !!reading,
    queryFn: () => agentRequest<AgentSkillResourceResponse>(`/agent/sessions/${encodeURIComponent(sessionId!)}/skills/${encodeURIComponent(reading!.name)}/resources?${new URLSearchParams({ path: reading!.path, offset: String(reading!.offset), limit: '4000' })}`) })
  const refreshPending = () => {
    const prefix = `clear-agent-skill-command:${workspace}:${sessionId}:`
    const commands: typeof pending = []
    for (let index = 0; index < sessionStorage.length; index++) {
      const key = sessionStorage.key(index)
      if (!key?.startsWith(prefix)) continue
      try {
        const command = JSON.parse(sessionStorage.getItem(key)!) as { stamp: string }
        const body = JSON.parse(command.stamp) as { name: string; version?: string }
        const action = key.slice(key.lastIndexOf(':') + 1)
        if (typeof body.name === 'string' && ['activate', 'deactivate'].includes(action)) commands.push({ ...body, action: action as 'activate' | 'deactivate' })
      } catch { /* Ignore malformed local records; they cannot authorize writes. */ }
    }
    setPending(commands)
  }
  useEffect(() => { setOffset(0); setError(''); setReading(null); refreshPending() }, [workspace, sessionId, open])
  const change = useMutation({ mutationFn: async ({ skill, action }: { skill: AgentSkill; action: 'activate' | 'deactivate' }) => {
    if (!sessionId) throw new Error('先保存目标或开始任务，再为此任务启用技能。')
    const storageKey = `clear-agent-skill-command:${workspace}:${sessionId}:${skill.name}:${action}`
    const body = { name: skill.name, ...(action === 'activate' ? { version: skill.version } : {}) }
    const stamp = JSON.stringify(body)
    const stored = sessionStorage.getItem(storageKey)
    let attempt: { stamp: string; request_id: string } | null = null
    try { attempt = stored ? JSON.parse(stored) : null } catch { /* Invalid local state cannot authorize a command. */ }
    if (attempt && attempt.stamp !== stamp) throw new Error('此技能有待核实的原操作，请先重试原操作。')
    attempt ||= { stamp, request_id: requestId() }
    sessionStorage.setItem(storageKey, JSON.stringify(attempt))
    refreshPending()
    const response = await agentWrite<{ active: AgentSkill[]; skill: AgentSkill; changed: boolean }>(`/agent/sessions/${encodeURIComponent(sessionId)}/skills/${action}`, { ...body, request_id: attempt.request_id })
    sessionStorage.removeItem(storageKey)
    return { response, action, sessionId, workspace }
  }, onSuccess: ({ action, response, sessionId: changedSession }) => {
    if (changedSession !== sessionId) return
    setError(''); refreshPending(); if (reading?.name === response.skill.name) setReading(null)
    void client.invalidateQueries({ queryKey: ['agent-skills', workspace, changedSession] })
    void client.invalidateQueries({ queryKey: ['agent-session', changedSession] })
  }, onError: caught => { setError(caught.message); refreshPending(); void listing.refetch() } })
  const active = listing.data?.active || []
  useEffect(() => {
    if (!reading || !listing.data) return
    const fixed = listing.data.active.find(item => item.name === reading.name)
    if (!fixed || (fixed.active_version || fixed.version) !== reading.version) setReading(null)
  }, [listing.data, reading])
  const read = (skill: AgentSkill) => { setReading({ name: skill.name, version: skill.active_version || skill.version, path: 'SKILL.md', offset: 0 }); setError('') }
  const controls = (skill: AgentSkill, fixed: boolean) => <Space wrap>
    {(fixed || skill.active) && <Button size="small" onClick={() => read(skill)}>查看正文与资源</Button>}
    {fixed ? <Button aria-label="停用" size="small" disabled={running || change.isPending || pending.some(command => command.name === skill.name)} onClick={() => change.mutate({ skill, action: 'deactivate' })}>停用</Button> :
      <Button aria-label={skill.active ? skill.source_changed ? '更新并激活' : '已激活' : '激活'} size="small" disabled={!sessionId || running || change.isPending || pending.some(command => command.name === skill.name) || !skill.compatible || skill.active && !skill.source_changed}
        onClick={() => change.mutate({ skill, action: 'activate' })}>{skill.active ? skill.source_changed ? '更新并激活' : '已激活' : '激活'}</Button>}
  </Space>
  const details = (skill: AgentSkill, fixed: boolean) => <section key={`${fixed ? 'fixed' : 'available'}:${skill.name}`} className="agent-skill-item" aria-label={`${fixed ? '已激活' : '可用'}技能 ${skill.name}`}>
    <div className="agent-skill-heading"><Typography.Text strong>{skill.name}</Typography.Text><Space size="small">
      {fixed && <Tag color="success">已激活</Tag>}<Tag color={skill.compatible ? 'default' : 'warning'}>{skill.compatible ? '兼容' : '不兼容'}</Tag>
    </Space></div>
    <Typography.Paragraph>{skill.description}</Typography.Paragraph>
    <Typography.Paragraph type="secondary">{sourceLabel(skill.source)} · {fixed ? '固定版本' : '当前版本'} {versionLabel(fixed ? skill.active_version || skill.version : skill.version)}</Typography.Paragraph>
    {skill.compatibility && <Typography.Paragraph type="secondary">兼容条件：{skill.compatibility}</Typography.Paragraph>}
    {!skill.compatible && <Typography.Paragraph type="warning">{skill.reason || '当前平台不满足兼容条件'}</Typography.Paragraph>}
    {skill.source_changed && <Alert type="info" title="源文件已有变化；已激活技能继续使用固定快照。" />}
    {controls(skill, fixed)}
  </section>
  const readingSkill = active.find(skill => skill.name === reading?.name)
  const files = readingSkill ? [{ path: 'SKILL.md', kind: 'skill' }, ...readingSkill.resources.filter(file => file.path !== 'SKILL.md')] : []
  const content = resource.data && !resource.data.unsupported ? resource.data : undefined
  const unsupported = resource.data?.unsupported ? resource.data : undefined
  return <Drawer title="Agent 技能" size={640} open={open} onClose={close}>
    <Typography.Paragraph>技能指导 Agent 编排已有工具。Agent 可按目标自行发现和激活技能，也可在这里为当前任务明确选择。</Typography.Paragraph>
    <Typography.Paragraph type="secondary">技能不会扩大资料授权或自动取得命令执行权限。正文及资源按需读取，初始不会全部占用上下文。</Typography.Paragraph>
    {!sessionId && <Alert type="info" title="先保存目标或开始任务，再为此任务启用技能。" />}
    {running && <Alert type="info" title="任务正在执行；停止或切换人工处理后可调整技能。" />}
    {(error || listing.error) && <Alert type="error" title={error || listing.error?.message} closable onClose={() => setError('')} />}
    {!!pending.length && <Alert type="warning" title="有技能操作的回包待核实" description={<Space direction="vertical">{pending.map(command => <Button key={`${command.name}:${command.action}`} disabled={running || change.isPending}
      onClick={() => { const original = [...(listing.data?.items || []), ...(listing.data?.active || [])].find(item => item.name === command.name)
        const replay = { ...(original || { name: command.name }), ...(command.version ? { version: command.version } : {}) } as AgentSkill
        change.mutate({ skill: replay, action: command.action }) }}>重试原{command.action === 'activate' ? '激活' : '停用'}操作 · {command.name}</Button>)}</Space>} />}
    <Typography.Title level={5}>当前任务已激活 {active.length} 项</Typography.Title>
    {active.length ? active.map(skill => details(skill, true)) : <Typography.Paragraph type="secondary">尚未激活；Agent 仍可根据目标选择。</Typography.Paragraph>}
    {reading && <section className="agent-skill-reader" aria-label="技能正文与资源">
      <div className="agent-skill-heading"><Typography.Text strong>{reading.name} · 固定版本 {versionLabel(reading.version)}</Typography.Text><Button type="text" size="small" onClick={() => setReading(null)}>关闭阅读</Button></div>
      <Select aria-label="技能资源" value={reading.path} style={{ width: '100%' }} options={files.map(file => ({ value: file.path, label: `${resourceLabel(file.kind)} · ${file.path}` }))}
        onChange={path => setReading({ ...reading, path, offset: 0 })} />
      {resource.error && <Alert type="error" title={resource.error.message} />}
      {resource.isFetching ? <Spin /> : unsupported ? <Alert type="info" title="此宿主不支持执行技能脚本" description={unsupported.reason} /> : content && <>
        {content.source_changed && <Typography.Paragraph type="secondary">源文件已有变化，以下为任务激活时保存的版本。</Typography.Paragraph>}
        {content.encoding === 'utf-8' ? <pre className="agent-skill-text">{content.content}</pre> : <Alert type="info" title="此资源为二进制文件，当前阅读器仅显示资源身份。" description={`SHA-256：${content.sha256}`} />}
        <Space><Button disabled={!reading.offset || resource.isFetching} onClick={() => setReading({ ...reading, offset: Math.max(0, reading.offset - 4000) })}>上一段</Button>
          <Button disabled={content.next_offset == null || resource.isFetching} onClick={() => setReading({ ...reading, offset: content.next_offset! })}>下一段</Button>
          <Typography.Text type="secondary">{reading.offset + 1}–{Math.min(reading.offset + content.content.length, content.total_chars)} / {content.total_chars} 字符</Typography.Text></Space>
      </>}
    </section>}
    <Typography.Title level={5}>可用技能目录</Typography.Title>
    <Input.Search aria-label="搜索技能" placeholder="按名称或用途搜索" value={keyword} onChange={event => { setKeyword(event.target.value); setOffset(0) }} allowClear />
    {listing.isPending ? <Spin /> : listing.data?.items.length ? listing.data.items.map(skill => details(skill, false)) : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有匹配的已登记技能" />}
    <Space><Button disabled={!offset || listing.isFetching} onClick={() => setOffset(Math.max(0, offset - 20))}>上一页</Button><Button disabled={listing.data?.next_offset == null || listing.isFetching} onClick={() => setOffset(listing.data!.next_offset!)}>下一页</Button>
      <Typography.Text type="secondary">共 {listing.data?.total || 0} 项</Typography.Text></Space>
    {!!listing.data?.rejected?.length && <details className="agent-skill-rejected"><summary>无法加载的登记项</summary>{listing.data.rejected.map((item, index) => <Typography.Paragraph key={index}>{sourceLabel(item.source)}：{item.reason}</Typography.Paragraph>)}</details>}
  </Drawer>
}
