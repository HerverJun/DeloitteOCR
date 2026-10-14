import { Alert, Card, Tag, Typography } from 'antd'
import type { ContextBudget } from './api'

const componentLabels: Record<string, string> = {
  system: '系统说明', environment: '执行环境', skills: '技能指导', tools: '工具契约', user: '用户消息',
  facts: '任务与来源事实', tool_results: '工具结果', assistant: 'Agent 历史', overhead: '协议开销',
}
const number = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN') : '未提供'
const method = (value?: string) => {
  if (!value) return '本地估计'
  if (value.includes('byte')) return `UTF-8 字节保守估计（${value}）`
  if (value.includes('token') || value.includes('tiktoken')) return `分词器估计（${value}）`
  return `本地估计（${value}）`
}

export function ContextBudgetContent({ budget, exceeded = false }: { budget: ContextBudget; exceeded?: boolean }) {
  const total = typeof budget.input_tokens_estimate === 'number' && typeof budget.output_reserve === 'number'
    ? budget.input_tokens_estimate + budget.output_reserve : undefined
  const entries = Object.entries(budget.component_tokens || {}).filter(([, value]) => typeof value === 'number')
  const overflow = typeof budget.overflow_component === 'string' ? budget.overflow_component : undefined
  const largest = overflow ? entries.find(([key]) => key === overflow) : [...entries].sort((a, b) => b[1] - a[1])[0]
  return <div className="agent-context-budget" aria-label="模型请求上下文预算">
    <div className="agent-context-summary"><Typography.Text strong>{exceeded ? '本地上下文检查未通过' : '本次请求上下文'}</Typography.Text>
      <Typography.Text>输入估计 {number(budget.input_tokens_estimate)} tokens · 输出预留 {number(budget.output_reserve)} tokens</Typography.Text>
      {typeof budget.context_cap === 'number' && <Typography.Text type="secondary">合计 {number(total)} / 本地上限 {number(budget.context_cap)} tokens</Typography.Text>}
    </div>
    {exceeded && largest && <Alert type="warning" title={`${overflow ? '超限组成' : '占用最多'}：${componentLabels[largest[0]] || largest[0]}，约 ${number(largest[1])} tokens`} description="这是请求组成的本地预算检查，普通短消息也可能因工具或技能内容过多而无法发送。" />}
    <details><summary>查看预算组成与工具加载</summary>
      <Typography.Paragraph type="secondary">{method(budget.estimation_method || (typeof budget.method === 'string' ? budget.method : undefined))}；估计值用于请求前预算，服务返回的实际用量另列。</Typography.Paragraph>
      <dl className="agent-context-components">{entries.map(([key, value]) => <div key={key}><dt>{componentLabels[key] || key}</dt><dd>{number(value)} tokens</dd></div>)}
        <div><dt>输出预留</dt><dd>{number(budget.output_reserve)} tokens</dd></div>
      </dl>
      <Typography.Paragraph type="secondary">最终请求 {number(budget.payload_bytes)} 字节{budget.compressed ? '；已压缩发送副本，原始记录保留。' : '。'}</Typography.Paragraph>
      <Typography.Paragraph>本轮可调用 {budget.selected_tools?.length || 0} 个工具{budget.reclaimed_tools?.length ? `，收回 ${budget.reclaimed_tools.length} 个无关契约` : ''}。</Typography.Paragraph>
      <div className="agent-context-tools">{budget.selected_tools?.map(name => <Tag key={name}>{name}</Tag>)}</div>
    </details>
  </div>
}

export function ModelRequestContent({ payload }: { payload: Record<string, unknown> }) {
  const budget = (payload.context_budget || payload.context_metrics) as ContextBudget | undefined
  if (!budget) return <Typography.Paragraph type="secondary">正在向 {String(payload.model || '主控模型')} 请求回应…</Typography.Paragraph>
  return <Card size="small" className="agent-step"><Typography.Paragraph type="secondary">主控请求 · 配置模型 {String(payload.configured_model || payload.model || '')}</Typography.Paragraph><ContextBudgetContent budget={budget} />
    {!!payload.injection && <EnvironmentContent payload={payload.injection as Record<string, unknown>} />}</Card>
}

export function ModelUsageContent({ payload }: { payload: Record<string, unknown> }) {
  return <Typography.Paragraph type="secondary" className="agent-usage" aria-label="服务返回实际用量">服务返回实际用量：输入 {number(payload.input_tokens)} tokens · 输出 {number(payload.output_tokens)} tokens
    {typeof payload.returned_model === 'string' && ` · 服务返回模型 ${payload.returned_model}`}</Typography.Paragraph>
}

export function EnvironmentContent({ payload }: { payload: Record<string, unknown> }) {
  // Only display registered environment facts. Never dump process environment or credential-bearing data.
  const environment = payload.environment && typeof payload.environment === 'object' ? payload.environment as Record<string, unknown> : payload
  const controller = environment.controller && typeof environment.controller === 'object' ? environment.controller as Record<string, unknown> : {}
  const fields: [string, unknown][] = [['配置模型', controller.configured_model || environment.configured_model || environment.model],
    ['接口协议', controller.protocol || environment.protocol], ['连接版本', controller.connection_revision || environment.connection_revision],
    ['业务执行环境', environment.execution_environment || environment.operating_system || environment.platform],
    ['环境版本', payload.environment_version || payload.version || environment.version], ['任务事实版本', payload.facts_version]]
  const injectedFields = Array.isArray(payload.environment_fields) ? payload.environment_fields.filter((value): value is string => typeof value === 'string') : []
  const skills = Array.isArray(payload.skills) ? payload.skills.filter((value): value is Record<string, unknown> => !!value && typeof value === 'object') : []
  return <details className="agent-environment"><summary>已注入平台环境与任务事实</summary>
    {fields.filter(([, value]) => typeof value === 'string' || typeof value === 'number').map(([label, value]) => <Typography.Paragraph key={label}>{label}：{String(value)}</Typography.Paragraph>)}
    {!!injectedFields.length && <Typography.Paragraph>已注入字段：{injectedFields.join('、')}</Typography.Paragraph>}
    {!!skills.length && <Typography.Paragraph>技能版本：{skills.map(skill => `${String(skill.name || '')} · ${String(skill.version || '').slice(0, 12)}${skill.source_changed ? '（源文件已有变化，使用固定快照）' : ''}`).join('；')}</Typography.Paragraph>}
    <Typography.Paragraph type="secondary">主控通过平台注册工具执行，OCR 与分类由专业服务处理。模型请求记录保留本轮注入版本。</Typography.Paragraph>
  </details>
}
