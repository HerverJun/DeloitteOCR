import { useEffect } from 'react'
import { Alert, Button, Form, Input, Modal, Switch, Typography } from 'antd'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { agentRequest, agentWrite } from './api'

type Server = { name: string; transport: 'stdio' | 'streamable-http'; enabled?: boolean; command?: string; args?: string[]; url?: string; env?: Record<string, string>; headers?: Record<string, string>; credential_configured?: boolean }
type Config = { revision: number; subagents: boolean; mcp_servers: Server[] }
type Draft = { subagents: boolean; servers: string }

export function IntegrationSettings({ open, close }: { open: boolean; close: () => void }) {
  const [form] = Form.useForm<Draft>()
  const client = useQueryClient()
  const current = useQuery({ queryKey: ['agent-integrations'], enabled: open,
    queryFn: () => agentRequest<Config>('/agent/integrations') })
  useEffect(() => {
    if (open && current.data) form.setFieldsValue({ subagents: current.data.subagents,
      servers: JSON.stringify(current.data.mcp_servers.map(({ credential_configured: _saved, ...server }) => server), null, 2) })
  }, [open, current.data, form])
  const save = useMutation({ mutationFn: (draft: Draft) => agentWrite<Config>('/agent/integrations', {
    revision: current.data?.revision || 0, subagents: draft.subagents, mcp_servers: JSON.parse(draft.servers),
  }, 'PUT'), onSuccess: () => { void client.invalidateQueries({ queryKey: ['agent-integrations'] }); close() } })
  return <Modal title="Agent 扩展" open={open} onCancel={close} footer={null} width={680} destroyOnHidden>
    <Typography.Paragraph>文件、搜索、PowerShell、Python、后台任务、技能脚本和网页读取已提供。专业能力仍按目标调用。</Typography.Paragraph>
    {(current.error || save.error) && <Alert type="error" title={(current.error || save.error)?.message} />}
    <Form form={form} layout="vertical" onFinish={values => save.mutate(values)}>
      <Form.Item name="subagents" label="按需启用子助手" valuePropName="checked" extra="主助手可委托独立工作，并在同一任务中收回结果。"><Switch /></Form.Item>
      <Form.Item name="servers" label="MCP 连接（JSON 数组）" extra="支持 stdio 和 streamable-http。本地 command 使用 F 盘可执行文件的绝对路径；HTTP 使用 url。凭据可放在 env 或 headers，保存后本机加密，留空沿用原凭据。修改将在新一轮生效；已有后台作业运行时，当前会话先保留原连接。"
        rules={[{ required: true }, { validator: async (_rule, value: string) => {
          try { if (!Array.isArray(JSON.parse(value))) throw new Error() } catch { throw new Error('请输入有效的 JSON 数组；不配置时填 []') }
        } }]}><Input.TextArea rows={11} spellCheck={false} aria-label="MCP连接配置" placeholder={'[{"name":"calculator","transport":"streamable-http","url":"http://127.0.0.1:9000/mcp"}]'} /></Form.Item>
      {current.data?.mcp_servers.some(server => server.credential_configured) && <Typography.Paragraph type="secondary">已有连接的凭据已加密保存，不在此处显示。删除连接并保存可移除该连接配置。</Typography.Paragraph>}
      <Button type="primary" htmlType="submit" loading={save.isPending} disabled={!current.data}>保存扩展设置</Button>
    </Form>
  </Modal>
}
