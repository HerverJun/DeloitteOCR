import { useEffect, useState } from 'react'
import { Alert, Button, Form, Input, InputNumber, Modal, Select, Space, Switch, Typography } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { agentRequest, agentWrite, type Controller } from './api'
import { ContextBudgetContent } from './ContextBudget'
import type { ControllerDraft } from '../../../../../packages/contracts/types.generated'

export function ControllerSettings({ open, close }: { open: boolean; close: () => void }) {
  const [form] = Form.useForm<ControllerDraft>()
  const [models, setModels] = useState<{ id: string }[]>([])
  const [saved, setSaved] = useState<Controller | null>(null)
  const client = useQueryClient()
  const current = useQuery({ queryKey: ['agent-connection'], enabled: open,
    queryFn: () => agentRequest<Controller>('/agent/connection') })
  useEffect(() => {
    if (open && current.data) {
      const { protocol, base_url, model, token_parameter, context_cap, max_output_tokens, total_seconds } = current.data
      form.setFieldsValue({ protocol: (protocol || 'openai_chat_completions') as ControllerDraft['protocol'],
        base_url: base_url || '', model: model || '', token_parameter: (token_parameter || 'max_completion_tokens') as ControllerDraft['token_parameter'],
        context_cap: context_cap || 16384, max_output_tokens: max_output_tokens || 2048,
        total_seconds: total_seconds || 180, api_key: '', streaming: current.data.streaming ?? true })
    }
  }, [open, current.data, form])
  useEffect(() => { if (!open) setSaved(null) }, [open])
  const save = useMutation({ mutationFn: (values: ControllerDraft) => agentWrite<Controller>('/agent/connection', values, 'PUT'),
    onSuccess: result => { form.setFieldValue('api_key', ''); setSaved(result); void client.invalidateQueries({ queryKey: ['agent-connection'] }) } })
  const listing = useMutation({ mutationFn: async () => {
    const values = form.getFieldsValue(true)
    return agentWrite<{ models: { id: string }[] }>('/agent/connection/models', { ...values, model: values.model || 'model-list-probe' })
  }, onSuccess: result => setModels(result.models) })
  const preflight = saved?.preflight || saved?.capabilities?.production_preflight
  const budget = preflight?.context_budget || preflight?.context_metrics
  const streaming = saved?.capabilities?.streaming || current.data?.capabilities?.streaming
  return <Modal title="主控连接" open={open} onCancel={close} footer={null} destroyOnHidden afterClose={() => form.resetFields()}>
    <Typography.Paragraph type="secondary">主控根据目标与工具结果选择下一步。OCR 和分类继续使用已有的专业模型。</Typography.Paragraph>
    {(save.error || current.error || listing.error) && <Alert type="error" title={(save.error || current.error || listing.error)?.message} showIcon />}
    {saved && <Alert type="success" title="主控连接已测试并保存" description="工具调用探针已通过；首轮上下文预算检查采用本地配置，不代表服务真实上下文容量。" />}
    {(budget || streaming) && <details className="agent-connection-diagnostics"><summary>连接诊断</summary>{budget && <ContextBudgetContent budget={budget} />}
    {streaming && <div className="agent-streaming-capability"><Typography.Paragraph type="secondary">流式配置：{streaming.requested ? '开启' : '关闭'} · 实测响应：{({ sse: 'SSE', json: '完整 JSON', disabled: '未请求流式', unknown: '未知' })[streaming.mode]}</Typography.Paragraph>
      {streaming.tested && <Typography.Paragraph type="secondary">公开正文预览：{streaming.text?.preview_observed ? '已观察' : '未观察'} · 答案工具参数预览：{streaming.answer?.preview_observed ? '已观察' : '未观察'}{streaming.detected_at ? ` · 检测于 ${new Date(streaming.detected_at).toLocaleString('zh-CN')}` : ''}</Typography.Paragraph>}
      {streaming.mode === 'json' && <Typography.Paragraph type="secondary">此连接以完整结果返回，等待期间仍显示真实活动；不会模拟逐字输出。</Typography.Paragraph>}
    </div>}</details>}
    <Form form={form} layout="vertical" onFinish={values => { setSaved(null); save.mutate(values) }} preserve={false}>
      <Form.Item name="base_url" label="API 地址" rules={[{ required: true }]}><Input placeholder="https://服务地址/v1" autoComplete="off" /></Form.Item>
      <Form.Item name="model" label="模型" rules={[{ required: true }]}><Input placeholder="模型 ID" autoComplete="off" /></Form.Item>
      <Space direction="vertical" style={{ width: '100%', marginBottom: 16 }}>
        <Button loading={listing.isPending} onClick={() => listing.mutate()}>读取服务模型列表</Button>
        {models.length > 0 && <Select aria-label="服务模型列表" style={{ width: '100%' }} placeholder="选择模型，也可以手填ID"
          options={models.map(model => ({ value: model.id, label: model.id }))} onChange={model => form.setFieldValue('model', model)} />}
      </Space>
      <Form.Item name="api_key" label="API Key" extra={current.data?.configured ? '留空保留当前凭据；更换地址或协议时需重新填写。' : '凭据由本机 Windows 加密保存。本地匿名服务可填 local。'}>
        <Input.Password autoComplete="new-password" /></Form.Item>
      <details className="agent-connection-advanced"><summary>高级连接参数</summary>
      <Form.Item name="protocol" label="接口协议" rules={[{ required: true }]}>
        <Select options={[{ value: 'openai_chat_completions', label: 'OpenAI 兼容工具调用' }, { value: 'anthropic_messages', label: 'Anthropic Messages' }]} />
      </Form.Item>
      <Form.Item name="token_parameter" label="输出长度参数"><Select options={[{ value: 'max_completion_tokens', label: 'max_completion_tokens' }, { value: 'max_tokens', label: 'max_tokens' }]} /></Form.Item>
      <Form.Item name="streaming" label="逐步显示模型回应" valuePropName="checked" extra="服务不支持流式工具协议时可关闭。"><Switch /></Form.Item>
      <Space align="start" wrap>
        <Form.Item name="context_cap" label="本地上下文预算"><InputNumber min={4096} max={200000} /></Form.Item>
        <Form.Item name="max_output_tokens" label="单次输出上限"><InputNumber min={256} max={8192} /></Form.Item>
        <Form.Item name="total_seconds" label="超时（秒）"><InputNumber min={10} max={600} /></Form.Item>
      </Space>
      <Typography.Paragraph type="secondary">本地上下文预算用于限制每次请求的输入与输出预留，填写时参考服务说明；不会自动证明或改变模型的真实容量。</Typography.Paragraph>
      </details>
      <Typography.Paragraph type="secondary">测试发送随机探针，验证两轮工具调用及结果消费，并在本地检查生产首轮的上下文组成；通过后保存，不发送业务资料。</Typography.Paragraph>
      <Button type="primary" htmlType="submit" loading={save.isPending}>测试并保存</Button>
      {saved && <Button aria-label="完成" style={{ marginLeft: 8 }} onClick={close}>完成</Button>}
    </Form>
  </Modal>
}
