import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Form, Input, Popconfirm, Select, Space, Switch, Table, Typography } from 'antd'
import { taskJson, postJson, useSession, type SessionUser } from './session'

export function AccountSettings() {
  const { user, multiuser } = useSession(); const client = useQueryClient()
  const [workspace, setWorkspace] = useState<string | undefined>()
  const [notice, setNotice] = useState('')
  const users = useQuery({ queryKey: ['central-users'], enabled: multiuser && !!user?.is_admin, queryFn: () => taskJson<{ items: SessionUser[] }>('/api/platform/v2/users') })
  const projects = useQuery({ queryKey: ['central-projects'], enabled: multiuser, queryFn: () => taskJson<{ items: { workspace_id: string; name: string }[] }>('/api/platform/v2/workspaces') })
  const members = useQuery({ queryKey: ['central-members', workspace], enabled: !!workspace, queryFn: () => taskJson<{ items: { user_id: string; display_name: string; username: string; role: string }[] }>(`/api/platform/v2/workspaces/${workspace}/members`) })
  const create = useMutation({ mutationFn: (value: { username: string; display_name: string; password: string; is_admin?: boolean }) => taskJson('/api/platform/v2/users', postJson({ ...value, is_admin: value.is_admin || false })), onSuccess: () => { setNotice('账号已创建'); void client.invalidateQueries({ queryKey: ['central-users'] }) }, onError: error => setNotice(error.message) })
  const grant = useMutation({ mutationFn: (value: { user_id: string; role: string }) => taskJson(`/api/platform/v2/workspaces/${workspace}/members`, { ...postJson(value), method: 'PUT' }), onSuccess: () => { setNotice('项目权限已保存'); void client.invalidateQueries({ queryKey: ['central-members', workspace] }) }, onError: error => setNotice(error.message) })
  const canManage = user?.is_admin || members.data?.items.some(row => row.user_id === user?.id && row.role === 'owner')
  const memberOptions = useQuery({ queryKey: ['central-member-options', workspace], enabled: !!workspace && !!canManage, queryFn: () => taskJson<{ items: SessionUser[] }>(`/api/platform/v2/workspaces/${workspace}/member-options`) })
  const disable = useMutation({ mutationFn: (row: SessionUser) => taskJson(`/api/platform/v2/users/${row.id}`, { ...postJson({ disabled: !row.disabled }), method: 'PATCH' }), onSuccess: () => { setNotice('账号状态已更新'); void client.invalidateQueries({ queryKey: ['central-users'] }); void client.invalidateQueries({ queryKey: ['central-member-options'] }) }, onError: error => setNotice(error.message) })
  const revoke = useMutation({ mutationFn: (id: string) => taskJson(`/api/platform/v2/workspaces/${workspace}/members/${id}`, { method: 'DELETE' }), onSuccess: () => { setNotice('成员权限已撤销'); void client.invalidateQueries({ queryKey: ['central-members', workspace] }) }, onError: error => setNotice(error.message) })
  if (!multiuser) return <Alert type="info" title="当前为单机模式，账号与项目权限在多人部署中启用。" />
  return <section><Typography.Title level={1}>账号与项目权限</Typography.Title>
    {notice && <Alert type={create.isError || grant.isError || revoke.isError || disable.isError ? 'error' : 'success'} title={notice} />}
    {user?.is_admin && <><Typography.Title level={3}>创建账号</Typography.Title><Form layout="inline" onFinish={value => create.mutate(value)}>
      <Form.Item name="username" rules={[{ required: true }]}><Input placeholder="账号" autoComplete="off" /></Form.Item>
      <Form.Item name="display_name" rules={[{ required: true }]}><Input placeholder="显示名称" /></Form.Item>
      <Form.Item name="password" rules={[{ required: true, min: 12 }]}><Input.Password placeholder="至少 12 位密码" autoComplete="new-password" /></Form.Item>
      <Form.Item name="is_admin" valuePropName="checked"><Switch checkedChildren="平台管理员" unCheckedChildren="普通账号" /></Form.Item>
      <Button htmlType="submit" type="primary" loading={create.isPending}>创建账号</Button></Form>
      <Table rowKey="id" size="small" loading={users.isPending} dataSource={users.data?.items} columns={[{ title: '账号', dataIndex: 'username' }, { title: '姓名', dataIndex: 'display_name' }, { title: '平台管理员', dataIndex: 'is_admin', render: value => value ? '是' : '否' }, { title: '状态', dataIndex: 'disabled', render: value => value ? '已停用' : '正常' }, { title: '操作', render: (_, row) => row.id !== user?.id && <Popconfirm title={row.disabled ? '恢复此账号登录？' : '停用此账号并立即撤销其访问？'} onConfirm={() => disable.mutate(row)}><Button type="link" danger={!row.disabled} loading={disable.isPending}>{row.disabled ? '启用' : '停用'}</Button></Popconfirm> }]} /></>}
    <Typography.Title level={3}>项目成员</Typography.Title><Select placeholder="选择项目" aria-label="权限管理项目" value={workspace} onChange={setWorkspace} style={{ minWidth: 280 }} options={projects.data?.items.map(row => ({ value: row.workspace_id, label: row.name }))} />
    {workspace && <><Table rowKey="user_id" size="small" loading={members.isPending} dataSource={members.data?.items} columns={[{ title: '账号', dataIndex: 'username' }, { title: '姓名', dataIndex: 'display_name' }, { title: '项目角色', dataIndex: 'role', render: value => ({ owner: '项目管理员', editor: '编辑者', viewer: '查看者' })[value as 'owner'] || value }, { title: '操作', render: (_, row) => canManage && <Popconfirm title="撤销此成员的项目访问权限？" onConfirm={() => revoke.mutate(row.user_id)}><Button type="link" danger loading={revoke.isPending}>撤销权限</Button></Popconfirm> }]} />
      {canManage && <Form layout="inline" onFinish={value => grant.mutate(value)}><Form.Item name="user_id" rules={[{ required: true }]}><Select placeholder="选择账号" style={{ minWidth: 220 }} loading={memberOptions.isFetching} options={memberOptions.data?.items.map(row => ({ value: row.id, label: row.display_name + ' · ' + row.username }))} /></Form.Item>
        <Form.Item name="role" initialValue="viewer"><Select options={[{ value: 'owner', label: '项目管理员' }, { value: 'editor', label: '编辑者' }, { value: 'viewer', label: '查看者' }]} /></Form.Item><Button htmlType="submit" loading={grant.isPending}>保存权限</Button></Form>}
    </>}
    {(users.isError || projects.isError || members.isError) && <Alert type="error" title="权限信息读取失败，请刷新重试" />}
    <Space className="task-controls"><a href="/jobs">返回任务中心</a></Space>
  </section>
}
