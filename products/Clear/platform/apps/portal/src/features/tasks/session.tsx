import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { Alert, Button, Form, Input, Skeleton, Space, Typography } from 'antd'
import { queryClient } from '../../queryClient'

export type SessionUser = { id: string; username: string; display_name: string; is_admin: boolean; disabled?: boolean }
type Session = { multiuser: boolean; user: SessionUser | null }
const SessionContext = createContext<Session>({ multiuser: false, user: null })
export const useSession = () => useContext(SessionContext)
let csrfToken = ''
let sessionFetch: typeof window.fetch | undefined

export function installSessionFetch() {
  if (window.fetch === sessionFetch) return
  const originalFetch = window.fetch.bind(window)
  sessionFetch = async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), window.location.origin)
    const method = init?.method || (input instanceof Request ? input.method : 'GET')
    const headers = new Headers(input instanceof Request ? input.headers : undefined)
    new Headers(init?.headers).forEach((value, name) => headers.set(name, value))
    if (url.origin === window.location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase())) {
      const cookie = document.cookie.split('; ').find(item => item.startsWith('clear_csrf='))?.slice(11)
      if (csrfToken || cookie) headers.set('X-Clear-CSRF', csrfToken || decodeURIComponent(cookie!))
    }
    if (url.origin === window.location.origin && url.pathname.startsWith('/api/platform/')) {
      const workspace = new URLSearchParams(window.location.search).get('workspace')
      if (workspace) headers.set('X-Clear-Workspace', workspace)
    }
    const response = await originalFetch(input, { ...init, headers })
    if (response.status === 401 && !url.pathname.endsWith('/auth/login')) window.dispatchEvent(new Event('clear-session-expired'))
    return response
  }
  window.fetch = sessionFetch
}

export async function taskJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { Accept: 'application/json', ...init?.headers } })
  const value = await response.json()
  if (!response.ok) throw new Error(value.detail || `请求失败（${response.status}）`)
  return value as T
}
export function postJson(value: unknown): RequestInit { return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) } }

export function SessionGate({ children }: { children: ReactNode }) {
  const [ready, setReady] = useState(false)
  const [multiuser, setMultiuser] = useState(false)
  const [user, setUser] = useState<SessionUser | null>(null)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  useEffect(() => {
    let mounted = true
    void Promise.resolve().then(() => fetch('/api/platform/v2/mode')).then(async response => {
      if (!response.ok) return
      const mode = await response.json()
      if (!mode.multiuser || !mounted) return
      setMultiuser(true); installSessionFetch()
      const me = await fetch('/api/platform/v2/auth/me')
      if (me.ok && mounted) setUser((await me.json()).user)
    }).catch(() => { /* The existing single-user portal has no v2 endpoint. */ }).finally(() => { if (mounted) setReady(true) })
    const expired = () => { setUser(null); queryClient.clear() }
    window.addEventListener('clear-session-expired', expired)
    return () => { mounted = false; window.removeEventListener('clear-session-expired', expired) }
  }, [])
  async function login(value: { username: string; password: string }) {
    setPending(true); setError('')
    try {
      const result = await taskJson<{ user: SessionUser; csrf_token: string }>('/api/platform/v2/auth/login', postJson(value))
      csrfToken = result.csrf_token; setUser(result.user); queryClient.clear()
    } catch (failure) { setError(failure instanceof Error ? failure.message : '登录失败') } finally { setPending(false) }
  }
  if (!ready) return <main className="task-login"><Skeleton active title paragraph={{ rows: 4 }} /></main>
  if (multiuser && !user) return <main className="task-login"><Typography.Text className="eyebrow">CLEAR</Typography.Text>
    <Typography.Title level={1}>登录审计平台</Typography.Title><Typography.Paragraph type="secondary">使用管理员为你创建的账号。</Typography.Paragraph>
    <Form layout="vertical" onFinish={login} requiredMark={false}>
      <Form.Item label="账号" name="username" rules={[{ required: true, message: '请输入账号' }]}><Input autoComplete="username" /></Form.Item>
      <Form.Item label="密码" name="password" rules={[{ required: true, message: '请输入密码' }]}><Input.Password autoComplete="current-password" /></Form.Item>
      {error && <Alert type="error" title={error} showIcon />}
      <Button htmlType="submit" type="primary" block loading={pending}>登录</Button>
    </Form></main>
  return <SessionContext.Provider value={{ multiuser, user }}>{multiuser && user && <aside className="task-session-bar">
    <Space><Typography.Text>{user.display_name}</Typography.Text><a href="/settings/accounts">账号与项目权限</a>
      <Button size="small" onClick={() => void taskJson('/api/platform/v2/auth/logout', postJson({})).then(() => { csrfToken = ''; setUser(null); queryClient.clear() })}>退出登录</Button></Space>
  </aside>}{children}</SessionContext.Provider>
}
