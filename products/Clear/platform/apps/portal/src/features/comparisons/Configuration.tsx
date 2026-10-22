import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Alert, Button, Checkbox, Form, Input, InputNumber, Modal, Select, Space, Table, Tag, Typography } from 'antd'
import { comparisonsApi, type ComparisonField, type ComparisonProfile, type LayoutParseConfig, type ProviderConnection } from './api'

export function ProviderSelect({ label, value, version, connections, onChange, vision = false }: { label: string; value: string | null; version?: number | null; connections: ProviderConnection[];
  onChange: (value: ProviderConnection) => void; vision?: boolean }) {
  const current = connections.find(item => item.id === value), selectedVersion = version || current?.version
  const options = connections.filter(item => !vision || item.supports_vision).map(item => ({ value: `${item.id}@${item.version}`, label: `${item.name} · ${item.model} · v${item.version}` }))
  if (current && selectedVersion && selectedVersion !== current.version) options.push({ value: `${current.id}@${selectedVersion}`, label: `${current.name} · 已锁定历史版本 v${selectedVersion}` })
  return <Form.Item label={label}><Select aria-label={label} value={value && selectedVersion ? `${value}@${selectedVersion}` : undefined} placeholder="选择已保存的连接" showSearch optionFilterProp="label"
    options={options} onChange={key => { const selected = connections.find(item => `${item.id}@${item.version}` === key); if (selected) onChange(selected) }} /></Form.Item>
}

export function LayoutOptions({ label, value, onChange, connections }: { label: string; value: LayoutParseConfig; onChange: (value: LayoutParseConfig) => void; connections: ProviderConnection[] }) {
  const update = (change: Partial<LayoutParseConfig>) => onChange({ ...value, ...change })
  const provider = connections.find(item => item.id === value.provider_id)
  return <Form layout="vertical" className="comparison-layout-options">
    <Form.Item label={`${label}解析方式`}><Select aria-label={`${label}解析方式`} value={value.mode} onChange={mode => update({ mode })}
      options={[{ value: 'local_ocr', label: '本地 OCR' }, { value: 'multimodal', label: '多模态 API · 一次一页' }]} /></Form.Item>
    {(value.mode === 'local_ocr' || value.fallback_local) && <Form.Item label={`${label}本地引擎`}><Select aria-label={`${label}本地引擎`} value={value.engine}
      onChange={engine => update({ engine })} options={[{ value: 'paddlevl', label: 'PaddleOCR-VL · 完整版面解析' }, { value: 'mineru_pro', label: 'MinerU Pro · 增强解析' }]} /></Form.Item>}
    {value.mode === 'multimodal' && <>
      <ProviderSelect label={`${label}逐页解析模型`} value={value.provider_id} version={value.provider_version} connections={connections} vision onChange={item => update({ provider_id: item.id,
        provider_version: item.version, max_output_tokens: Math.min(value.max_output_tokens, item.max_output_tokens) })} />
      <Typography.Paragraph type="secondary">每次只发送当前页图片，页面之间不累积对话。页图会发往所选供应商{provider ? `（${provider.base_url}）` : ''}。</Typography.Paragraph>
      <details><summary>单页预算与失败处理</summary><div className="comparison-budget-grid">
        <Form.Item label="单页最大输出 token"><InputNumber aria-label={`${label}单页最大输出 token`} min={256} max={provider?.max_output_tokens || 131072}
          value={value.max_output_tokens} onChange={number => number != null && update({ max_output_tokens: number })} /></Form.Item>
        <Form.Item label="图片最长边 px"><InputNumber aria-label={`${label}图片最长边`} min={512} max={8192} value={value.max_image_edge} onChange={number => number != null && update({ max_image_edge: number })} /></Form.Item>
        <Form.Item label="单页图片上限 MiB"><InputNumber aria-label={`${label}图片大小上限`} min={1} max={8} value={value.image_max_bytes / 1048576} onChange={number => number != null && update({ image_max_bytes: Math.floor(number * 1048576) })} /></Form.Item>
        <Form.Item label="每图块视觉 token 估计"><InputNumber aria-label={`${label}视觉 token 估计`} min={64} max={8192} value={value.vision_tokens_per_tile} onChange={number => number != null && update({ vision_tokens_per_tile: number })} /></Form.Item>
      </div><Checkbox checked={value.fallback_local} onChange={event => update({ fallback_local: event.target.checked })}>失败页使用本地 OCR 补全</Checkbox>
      <Typography.Paragraph type="secondary">输出截断会标记为不完整；未知调用结果停止自动重发。默认关闭本地补全。</Typography.Paragraph></details>
    </>}
  </Form>
}

const newConnection = (): Partial<ProviderConnection> & { api_key: string } => ({ name: '', protocol: 'openai_chat_completions', base_url: '', model: '', api_key: '',
  context_window: 32768, max_output_tokens: 8192, supports_vision: false, structured_output: 'prompt', max_request_bytes: 12 * 1048576, total_seconds: 180 })

export function ConnectionSettings({ workspace, connections, close, saved }: { workspace: string; connections: ProviderConnection[]; close: () => void; saved: () => void }) {
  const [draft, setDraft] = useState(newConnection), [notice, setNotice] = useState('')
  const update = (change: Partial<typeof draft>) => setDraft(previous => ({ ...previous, ...change }))
  const save = useMutation({ mutationFn: () => comparisonsApi.saveConnection(workspace, draft), onSuccess: connection => {
    setDraft({ ...connection, api_key: '' }); setNotice(`已保存 ${connection.name} v${connection.version}`); saved()
  } })
  const probe = useMutation({ mutationFn: (id: string) => comparisonsApi.probe(workspace, id, draft.version) })
  return <Modal title="模型连接与能力" open width={760} onCancel={close} footer={<Space><Button onClick={close}>关闭</Button>
    <Button type="primary" loading={save.isPending} disabled={!draft.name?.trim() || !draft.base_url?.trim() || !draft.model?.trim() || (draft.max_output_tokens || 0) >= (draft.context_window || 0)} onClick={() => save.mutate()}>保存连接版本</Button></Space>}>
    <Typography.Paragraph type="secondary">逐页解析和主题比较可分别选择连接。密钥只提交到 Clear 密钥库；留空沿用当前连接的密钥。</Typography.Paragraph>
    <Space wrap><Select aria-label="编辑已保存连接" placeholder="编辑已保存连接" value={draft.id} style={{ minWidth: 250 }}
      options={connections.map(item => ({ value: item.id, label: `${item.name} · v${item.version}` }))} onChange={id => { const item = connections.find(connection => connection.id === id); if (item) { setDraft({ ...item, api_key: '' }); setNotice(''); probe.reset() } }} />
      <Button onClick={() => { setDraft(newConnection()); setNotice(''); probe.reset() }}>新增连接</Button>
      {draft.id && <Button loading={probe.isPending} onClick={() => probe.mutate(draft.id!)}>检测已保存连接</Button>}</Space>
    {notice && <Alert type="success" title={notice} />}
    {(save.error || probe.error) && <Alert type="error" title={(save.error || probe.error)?.message} />}
    {probe.data && <Alert type={probe.data.passed ? 'success' : 'warning'} title={probe.data.message || probe.data.detail || (probe.data.passed ? `连接检测通过${probe.data.vision_tested ? ' · 已验证图片输入' : ''}` : '连接检测未通过')} />}
    <Form layout="vertical" className="comparison-connection-form"><div className="comparison-budget-grid">
      <Form.Item label="连接名称"><Input aria-label="连接名称" value={draft.name} onChange={event => update({ name: event.target.value })} /></Form.Item>
      <Form.Item label="协议"><Select aria-label="连接协议" value={draft.protocol} onChange={protocol => update({ protocol })}
        options={[{ value: 'openai_chat_completions', label: 'OpenAI Chat Completions 兼容' }, { value: 'anthropic_messages', label: 'Anthropic Messages' }]} /></Form.Item>
    </div>
      <Form.Item label="API Base URL"><Input aria-label="API Base URL" placeholder="https://api.example.com/v1" value={draft.base_url} onChange={event => update({ base_url: event.target.value })} /></Form.Item>
      <div className="comparison-budget-grid"><Form.Item label="模型名"><Input aria-label="模型名" value={draft.model} onChange={event => update({ model: event.target.value })} /></Form.Item>
        <Form.Item label={`API Key${draft.has_key ? '（已保存在密钥库）' : ''}`}><Input.Password aria-label="API Key" autoComplete="new-password" value={draft.api_key} onChange={event => update({ api_key: event.target.value })} /></Form.Item>
        <Form.Item label="上下文窗口 token"><InputNumber aria-label="上下文窗口 token" min={2048} max={2000000} value={draft.context_window} onChange={value => value != null && update({ context_window: value })} /></Form.Item>
        <Form.Item label="模型最大输出 token"><InputNumber aria-label="模型最大输出 token" min={256} max={200000} value={draft.max_output_tokens} onChange={value => value != null && update({ max_output_tokens: value })} /></Form.Item>
        <Form.Item label="请求体上限 MiB"><InputNumber aria-label="请求体上限 MiB" min={1} max={32} value={(draft.max_request_bytes || 12582912) / 1048576} onChange={value => value != null && update({ max_request_bytes: value * 1048576 })} /></Form.Item>
        <Form.Item label="调用超时（秒）"><InputNumber aria-label="调用超时" min={10} max={1800} value={draft.total_seconds || 180} onChange={value => value != null && update({ total_seconds: value })} /></Form.Item></div>
      {(draft.max_output_tokens || 0) >= (draft.context_window || 0) && <Alert type="warning" title="输出上限必须小于上下文容量。" />}
      <Space wrap><Checkbox checked={draft.supports_vision} onChange={event => update({ supports_vision: event.target.checked })}>支持图片输入</Checkbox>
        <Select aria-label="结构化输出模式" value={draft.structured_output} onChange={structured_output => update({ structured_output })} options={[{ value: 'prompt', label: '提示词约束 JSON' }, { value: 'json_object', label: 'JSON Object 模式' }, { value: 'json_schema', label: 'JSON Schema 模式' }]} /></Space>
    </Form>
  </Modal>
}

export function ProfileEditor({ value, onChange, onValidityChange }: { value: ComparisonProfile; onChange: (value: ComparisonProfile) => void; onValidityChange?: (valid: boolean) => void }) {
  const [aliasesError, setAliasesError] = useState('')
  const edit = (index: number, change: Partial<ComparisonField>) => onChange({ ...value, fields: value.fields.map((field, i) => i === index ? { ...field, ...change } : field) })
  return <>
    <Form layout="vertical"><div className="comparison-budget-grid"><Form.Item label="配置名称"><Input aria-label="配置名称" value={value.name} onChange={event => onChange({ ...value, name: event.target.value })} /></Form.Item>
      <Form.Item label="日期口径"><Tag>{value.date_policy === 'ignore_as_of' ? '忽略双方截止日期' : '相同截止日期才直接比较进度'}</Tag></Form.Item></div>
      <Form.Item label="比较要求"><Input.TextArea aria-label="比较要求" value={value.instructions} autoSize={{ minRows: 2, maxRows: 5 }} onChange={event => onChange({ ...value, instructions: event.target.value })} /></Form.Item>
      <div className="comparison-budget-grid"><Form.Item label="A 侧截止日期（可选）"><Input aria-label="A 侧截止日期" type="date" value={value.as_of_a || ''} onChange={event => onChange({ ...value, as_of_a: event.target.value || null })} /></Form.Item>
        <Form.Item label="B 侧截止日期（可选）"><Input aria-label="B 侧截止日期" type="date" value={value.as_of_b || ''} onChange={event => onChange({ ...value, as_of_b: event.target.value || null })} /></Form.Item></div></Form>
    <Table<ComparisonField> rowKey={(_, index) => String(index)} size="small" pagination={false} scroll={{ x: 740 }} dataSource={value.fields} columns={[
      { title: '标识', width: 145, render: (_, field, i) => <Input aria-label={`字段 ${i + 1} 标识`} value={field.key} onChange={event => edit(i, { key: event.target.value })} /> },
      { title: '字段名称', width: 125, render: (_, field, i) => <Input aria-label={`字段 ${i + 1} 名称`} value={field.label} onChange={event => edit(i, { label: event.target.value })} /> },
      { title: '类型', width: 120, render: (_, field, i) => <Select aria-label={`字段 ${i + 1} 类型`} value={field.type} onChange={type => edit(i, { type })} options={[{ value: 'text', label: '文字' }, { value: 'date', label: '日期' }, { value: 'number', label: '数字' }, { value: 'percentage', label: '百分比' }]} /> },
      { title: '字段别名（逗号分隔）', render: (_, field, i) => <Input key={field.aliases.join(',')} aria-label={`字段 ${i + 1} 别名`} defaultValue={field.aliases.join(', ')} onBlur={event => edit(i, { aliases: event.target.value.split(/[,，]/).map(item => item.trim()).filter(Boolean) })} /> },
      { title: '容差', width: 100, render: (_, field, i) => <InputNumber aria-label={`字段 ${i + 1} 容差`} min={0} value={field.tolerance} disabled={!['number', 'percentage'].includes(field.type)} onChange={tolerance => tolerance != null && edit(i, { tolerance })} /> },
      { title: '', width: 65, render: (_, _field, i) => <Button type="text" danger aria-label={`删除字段 ${i + 1}`} onClick={() => onChange({ ...value, fields: value.fields.filter((_, index) => i !== index) })}>删除</Button> },
    ]} />
    <Button className="comparison-add-field" onClick={() => onChange({ ...value, fields: [...value.fields, { key: `field_${value.fields.length + 1}`, label: '', type: 'text', aliases: [], tolerance: 0 }] })}>添加字段</Button>
    <details><summary>项目与工作项别名</summary><Typography.Paragraph type="secondary">输入 JSON，例如 {`{"项目 A": ["A 项目", "一期项目"]}`}。失去焦点后校验并保存到当前草稿。</Typography.Paragraph>
      <Input.TextArea key={JSON.stringify(value.entity_aliases)} aria-label="项目与工作项别名 JSON" defaultValue={JSON.stringify(value.entity_aliases, null, 2)} autoSize={{ minRows: 3, maxRows: 10 }} onBlur={event => {
        try { const parsed = JSON.parse(event.target.value); if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object' || Object.values(parsed).some(list => !Array.isArray(list) || list.some(item => typeof item !== 'string'))) throw new Error('格式无效')
          onChange({ ...value, entity_aliases: parsed }); setAliasesError(''); onValidityChange?.(true)
        } catch { setAliasesError('别名必须是对象，各值为字符串数组；当前输入尚未保存。'); onValidityChange?.(false) }
      }} />{aliasesError && <Alert type="error" title={aliasesError} />}</details>
  </>
}
