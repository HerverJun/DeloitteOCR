import { useEffect, useState } from 'react'
import { Alert, Button, Form, Input, InputNumber, Modal, Select, Space, Switch, Typography } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { agentRequest, agentWrite, type Controller, type ModelCandidate, type ModelCapabilitySource } from './api'
import { capacitySourceLabel, ContextBudgetContent } from './ContextBudget'
import type { ControllerDraft } from '../../../../../packages/contracts/types.generated'

export function ControllerSettings({ open, close }: { open: boolean; close: () => void }) {
  const [form] = Form.useForm<ControllerDraft>()
  const [models, setModels] = useState<ModelCandidate[]>([])
  const [modelsScope, setModelsScope] = useState('')
  const [saved, setSaved] = useState<Controller | null>(null)
  const client = useQueryClient()
  const current = useQuery({ queryKey: ['agent-connection'], enabled: open,
    queryFn: () => agentRequest<Controller>('/agent/connection') })
  useEffect(() => {
    if (open && current.data) {
      const { protocol, base_url, model, token_parameter, context_cap, runtime_context_cap, max_output_tokens, total_seconds,
        model_context_window, model_max_output_tokens, safety_margin_tokens, round_limit, tool_limit, active_seconds_limit } = current.data
      form.setFieldsValue({ protocol: (protocol || 'openai_chat_completions') as ControllerDraft['protocol'],
        base_url: base_url || '', model: model || '', token_parameter: (token_parameter || 'max_completion_tokens') as ControllerDraft['token_parameter'],
        context_cap: runtime_context_cap || context_cap || 65536, runtime_context_cap: runtime_context_cap || context_cap || 65536,
        model_context_window: model_context_window ?? null, model_max_output_tokens: model_max_output_tokens ?? null,
        safety_margin_tokens: safety_margin_tokens ?? null, round_limit: round_limit ?? null, tool_limit: tool_limit ?? null,
        active_seconds_limit: active_seconds_limit ?? null, max_output_tokens: max_output_tokens || 2048,
        total_seconds: total_seconds || 180, api_key: '', streaming: current.data.streaming ?? true,
        supports_images: current.data.supports_images ?? false })
    }
  }, [open, current.data, form])
  useEffect(() => { if (!open) { setSaved(null); setModels([]); setModelsScope('') } }, [open])
  const save = useMutation({ mutationFn: (values: ControllerDraft) => agentWrite<Controller>('/agent/connection', values, 'PUT'),
    onSuccess: result => { form.setFieldValue('api_key', ''); setSaved(result); void client.invalidateQueries({ queryKey: ['agent-connection'] }) } })
  const listing = useMutation({ mutationFn: async () => {
    const values = form.getFieldsValue(true)
    const result = await agentWrite<{ models: ModelCandidate[] }>('/agent/connection/models', { ...values, context_cap: values.runtime_context_cap || values.context_cap, model: values.model || 'model-list-probe' })
    return { ...result, scope: JSON.stringify([values.protocol, values.base_url]) }
  }, onSuccess: result => { setModels(result.models); setModelsScope(result.scope) } })
  const watched = Form.useWatch([], form) as ControllerDraft | undefined
  const sameIdentity = !!current.data && watched?.model === current.data.model && watched?.base_url === current.data.base_url && watched?.protocol === current.data.protocol
  const hasCurrentList = modelsScope === JSON.stringify([watched?.protocol, watched?.base_url])
  const metadata = hasCurrentList
    ? models.find(item => item.id === watched?.model) : sameIdentity ? current.data?.endpoint_model_metadata : undefined
  const profile = sameIdentity ? current.data?.model_profile : undefined
  const capability = (explicit: number | null | undefined, field: 'context_window' | 'max_output_tokens'): { value: number | null; source: ModelCapabilitySource } => {
    if (explicit) return { value: explicit, source: 'explicit_declaration' }
    if (metadata?.[field]) return { value: metadata[field]!, source: 'endpoint_metadata' }
    const source = profile?.capacity_sources[field]
    return source && source !== 'explicit_declaration' && (source !== 'endpoint_metadata' || !hasCurrentList)
      ? { value: (field === 'context_window' ? profile?.context_window : profile?.model_max_output_tokens) ?? null, source } : { value: null, source: 'unknown' }
  }
  const windowCapacity = capability(watched?.model_context_window, 'context_window'), outputCapacity = capability(watched?.model_max_output_tokens, 'max_output_tokens')
  const changeModel = (model: string) => {
    form.setFieldsValue({ model_context_window: null, model_max_output_tokens: null })
    form.setFieldValue('model', model)
    setSaved(null)
  }
  const preflight = saved?.preflight || saved?.capabilities?.production_preflight
  const budget = preflight?.context_budget || preflight?.context_metrics
  const streaming = saved?.capabilities?.streaming || current.data?.capabilities?.streaming
  return <Modal title="主控连接" open={open} onCancel={close} footer={null} width={680} destroyOnHidden afterClose={() => form.resetFields()}>
    <Typography.Paragraph type="secondary">主控根据目标与工具结果选择下一步。OCR 和分类继续使用已有的专业模型。</Typography.Paragraph>
    {(save.error || current.error || listing.error) && <Alert type="error" title={(save.error || current.error || listing.error)?.message} showIcon />}
    {saved && <Alert type="success" title="主控连接已测试并保存" description="工具调用探针已通过。运行会固定使用启动时的模型配置版本；能力声明与实际服务用量分别记录。" />}
    {(budget || streaming) && <details className="agent-connection-diagnostics"><summary>连接诊断</summary>{budget && <ContextBudgetContent budget={budget} />}
    {streaming && <div className="agent-streaming-capability"><Typography.Paragraph type="secondary">流式配置：{streaming.requested ? '开启' : '关闭'} · 实测响应：{({ sse: 'SSE', json: '完整 JSON', disabled: '未请求流式', unknown: '未知' })[streaming.mode]}</Typography.Paragraph>
      {streaming.tested && <Typography.Paragraph type="secondary">公开正文预览：{streaming.text?.preview_observed ? '已观察' : '未观察'} · 答案工具参数预览：{streaming.answer?.preview_observed ? '已观察' : '未观察'}{streaming.detected_at ? ` · 检测于 ${new Date(streaming.detected_at).toLocaleString('zh-CN')}` : ''}</Typography.Paragraph>}
      {streaming.mode === 'json' && <Typography.Paragraph type="secondary">此连接以完整结果返回，等待期间仍显示真实活动；不会模拟逐字输出。</Typography.Paragraph>}
    </div>}</details>}
    <Form form={form} layout="vertical" onValuesChange={changed => {
      if ('base_url' in changed || 'protocol' in changed) form.setFieldsValue({ model_context_window: null, model_max_output_tokens: null })
      setSaved(null)
    }} onFinish={values => { setSaved(null); save.mutate({ ...values, context_cap: values.runtime_context_cap || values.context_cap }) }} preserve={false}>
      <Form.Item name="base_url" label="API 地址" rules={[{ required: true }]}><Input placeholder="https://服务地址/v1" autoComplete="off" /></Form.Item>
      <Form.Item name="model" label="模型" rules={[{ required: true }]}><Input placeholder="模型 ID" autoComplete="off" onChange={event => changeModel(event.target.value)} /></Form.Item>
      <Space orientation="vertical" style={{ width: '100%', marginBottom: 16 }}>
        <Button loading={listing.isPending} onClick={() => listing.mutate()}>读取服务模型列表</Button>
        {hasCurrentList && models.length > 0 && <Select aria-label="服务模型列表" style={{ width: '100%' }} placeholder="选择模型，也可以手填ID"
          options={models.map(model => ({ value: model.id, label: `${model.name || model.id}${model.context_window ? ` · ${model.context_window.toLocaleString('zh-CN')} tokens` : ' · 容量未知'}` }))} onChange={changeModel} />}
      </Space>
      <div className="agent-model-capacity" aria-label="模型能力来源">
        <Typography.Text strong>模型能力</Typography.Text>
        <Typography.Paragraph>上下文容量：{windowCapacity.value ? `${windowCapacity.value.toLocaleString('zh-CN')} tokens` : '未知'} · {capacitySourceLabel(windowCapacity.source)}</Typography.Paragraph>
        <Typography.Paragraph>模型输出容量：{outputCapacity.value ? `${outputCapacity.value.toLocaleString('zh-CN')} tokens` : '未知'} · {capacitySourceLabel(outputCapacity.source)}</Typography.Paragraph>
        <Typography.Text type="secondary">读取列表后自动采用当前服务返回的能力元数据。网关别名只按当前服务解析；声明值尚未验证实际容量。</Typography.Text>
      </div>
      <Form.Item name="api_key" label="API Key" extra={current.data?.configured ? '留空保留当前凭据；更换地址或协议时需重新填写。' : '凭据由本机 Windows 加密保存。本地匿名服务可填 local。'}>
        <Input.Password autoComplete="new-password" /></Form.Item>
      <details className="agent-connection-advanced"><summary>高级连接参数</summary>
      <Form.Item name="protocol" label="接口协议" rules={[{ required: true }]}>
        <Select options={[{ value: 'openai_chat_completions', label: 'OpenAI 兼容工具调用' }, { value: 'anthropic_messages', label: 'Anthropic Messages' }]} />
      </Form.Item>
      <Form.Item name="token_parameter" label="输出长度参数"><Select options={[{ value: 'max_completion_tokens', label: 'max_completion_tokens' }, { value: 'max_tokens', label: 'max_tokens' }]} /></Form.Item>
      <Form.Item name="streaming" label="逐步显示模型回应" valuePropName="checked" extra="服务不支持流式工具协议时可关闭。"><Switch /></Form.Item>
      <Form.Item name="supports_images" label="主控支持图片输入" valuePropName="checked" extra="按所用模型的实际能力开启；Agent 可读取图片并发送给主控。OCR 专业识别仍可独立使用。"><Switch /></Form.Item>
      <div className="agent-settings-grid">
        <Form.Item name="model_context_window" label="声明模型上下文容量" extra="留空按服务元数据或匹配的模型目录解析。"><InputNumber aria-label="声明模型上下文容量" min={4096} precision={0} placeholder="自动解析" /></Form.Item>
        <Form.Item name="model_max_output_tokens" label="声明模型输出容量"><InputNumber aria-label="声明模型输出容量" min={1} precision={0} placeholder="自动解析" /></Form.Item>
        <Form.Item name="runtime_context_cap" label="本地上下文预算" rules={[{ required: true }]}><InputNumber aria-label="本地上下文预算" min={4096} precision={0} /></Form.Item>
        <Form.Item name="max_output_tokens" label="单次输出上限" rules={[{ required: true }]}><InputNumber aria-label="单次输出上限" min={256} precision={0} /></Form.Item>
        <Form.Item name="safety_margin_tokens" label="上下文安全余量"><InputNumber aria-label="上下文安全余量" min={0} precision={0} placeholder="自动预留" /></Form.Item>
        <Form.Item name="total_seconds" label="单次模型请求超时（秒）"><InputNumber min={10} max={600} precision={0} /></Form.Item>
      </div>
      <Typography.Paragraph type="secondary">本地上下文预算用于限制每次请求的输入与输出预留，填写时参考服务说明；不会自动证明或改变模型的真实容量。</Typography.Paragraph>
      <Typography.Paragraph type="secondary">有效窗口取模型容量与本地预算的较小值，再扣除输出和安全余量；模型容量未知时采用本地预算。输入达到可用预算的 80% 时自动整理。</Typography.Paragraph>
      <Typography.Text strong>持续执行预算</Typography.Text>
      <Typography.Paragraph type="secondary">留空表示不限，持续执行到完成、失败、被停止或需要输入。单次模型请求超时独立生效。</Typography.Paragraph>
      <div className="agent-settings-grid">
        <Form.Item name="round_limit" label="模型轮数上限"><InputNumber aria-label="模型轮数上限" min={1} precision={0} placeholder="不限" /></Form.Item>
        <Form.Item name="tool_limit" label="工具调用次数上限"><InputNumber aria-label="工具调用次数上限" min={1} precision={0} placeholder="不限" /></Form.Item>
        <Form.Item name="active_seconds_limit" label="运行时长上限（秒）"><InputNumber aria-label="运行时长上限" min={1} precision={0} placeholder="不限" /></Form.Item>
      </div>
      </details>
      <Typography.Paragraph type="secondary">测试发送随机探针，验证两轮工具调用及结果消费，并在本地检查生产首轮的上下文组成；通过后保存，不发送业务资料。</Typography.Paragraph>
      <Button type="primary" htmlType="submit" loading={save.isPending}>测试并保存</Button>
      {saved && <Button aria-label="完成" style={{ marginLeft: 8 }} onClick={close}>完成</Button>}
    </Form>
  </Modal>
}
