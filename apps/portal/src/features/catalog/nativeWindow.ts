import { useEffect, useRef, useState } from 'react'

export type NativeTarget = { url: string; kind: 'base' | 'ticket' | 'session'; baseUrl?: string }

export function platformHomeAddress(address: string, instanceId: string, platformOrigin: string, workspaceId: string | null): string {
  const id = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/
  if (!id.test(instanceId) || (workspaceId !== null && !id.test(workspaceId))) throw new Error('平台首页关联无效')
  const home = new URL(new URL(platformOrigin).origin === window.location.origin ? platformOrigin + '/' : nativeAddress({ url: platformOrigin + '/', kind: 'base' }))
  if (workspaceId) home.searchParams.set('workspace', workspaceId)
  const native = new URL(address)
  const hash = new URLSearchParams(native.hash.slice(1))
  hash.set('platform_home', home.href); hash.set('platform_instance', instanceId)
  native.hash = hash.toString()
  return native.href
}

export function nativeAddress(target: NativeTarget): string {
  const url = new URL(target.url)
  if (url.origin === window.location.origin && /^\/native\/[A-Za-z0-9_-]+\/(ocr|classification)\/[A-Za-z0-9_-]+\/$/.test(url.pathname)
    && !url.username && !url.password && !url.search && !url.hash) return url.href
  const port = Number(url.port)
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password
    || !Number.isInteger(port) || port < 1 || port > 65535 || url.pathname !== '/' || url.search)
    throw new Error('原生入口未通过本机校验')
  const expected = target.kind === 'ticket' ? /^#platform_ticket=[A-Za-z0-9_-]{32,128}$/
    : target.kind === 'session' ? /^#token=[A-Za-z0-9_-]{32,512}$/ : /^$/
  if (!expected.test(url.hash) || (target.baseUrl && url.origin !== new URL(target.baseUrl).origin))
    throw new Error('原生入口未通过本机校验')
  return url.href
}

export function useNativeWindow(scope: string) {
  const [opening, setOpening] = useState(false)
  const [error, setError] = useState('')
  const currentScope = useRef(scope)
  currentScope.current = scope
  const pending = useRef<{ popup: Window; controller: AbortController } | null>(null)
  useEffect(() => {
    setOpening(false); setError('')
    return () => {
      pending.current?.controller.abort()
      pending.current?.popup.close()
      pending.current = null
    }
  }, [scope])
  const open = (resolve: (signal: AbortSignal) => Promise<NativeTarget>) => {
    if (pending.current) return
    let popup: Window | null = null
    try { popup = window.open('', '_blank'); if (popup) popup.opener = null } catch { /* Show a recoverable error. */ }
    if (!popup) { setError('未能打开新窗口；请允许弹出窗口后重试。'); return }
    const operation = { popup, controller: new AbortController() }
    pending.current = operation
    const active = () => pending.current === operation && currentScope.current === scope
    let timedOut = false
    const timeout = setTimeout(() => {
      timedOut = true; operation.controller.abort(); popup!.close()
      if (active()) { pending.current = null; setOpening(false); setError('连接超时，请重新核实后打开。') }
    }, 60_000)
    setOpening(true); setError('')
    void Promise.resolve().then(() => resolve(operation.controller.signal)).then(target => {
      if (!active() || operation.controller.signal.aborted) return
      popup!.location.replace(nativeAddress(target))
      pending.current = null
    }).catch(failure => {
      popup!.close()
      if (active()) setError(timedOut ? '连接超时，请重新核实后打开。'
        : failure instanceof Error ? failure.message : '工作台连接暂不可用，请重试。')
    }).finally(() => {
      clearTimeout(timeout)
      if (pending.current === operation) { pending.current = null; setOpening(false) }
      else if (!pending.current && currentScope.current === scope) setOpening(false)
    })
  }
  return { open, opening, error }
}
