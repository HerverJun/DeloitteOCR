import { useEffect, useRef, useState } from 'react'
import { Alert, Button, Space } from 'antd'
import { AgentRequestError, agentWrite, requestId, type AgentSession } from './api'

export type PdfTextParameters = { page_numbers: number[]; mode: 'auto' | 'native' | 'ocr'; engine: 'ppocr' | 'paddlevl' | 'glm' | 'hunyuan' }
type Intent = { request_id: string; session_request_id: string; session_id: string | null; selected_assets: string[]; link_id?: string; expected_scope_revision?: number; parameters?: PdfTextParameters }
export function DirectImageTextButton({ workspace, sessionId, selectedAssets, scopeRevision, linkId, disabled = false, onAccepted, action = 'image-text', parameters, buttonType = 'default' }: {
  workspace: string; sessionId?: string | null; selectedAssets: string[]; scopeRevision?: number; linkId?: string; disabled?: boolean
  onAccepted: (sessionId: string) => void; action?: 'image-text' | 'pdf-text'; parameters?: PdfTextParameters
  buttonType?: 'default' | 'primary'
}) {
  const key = `clear-direct-${action}:${workspace}:${sessionId || 'new'}`
  const currentKey = useRef(key), mounted = useRef(true)
  currentKey.current = key
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  const live = () => mounted.current && currentKey.current === key
  const readPending = (): Intent | null => {
    try { const value = JSON.parse(localStorage.getItem(key) || 'null'); return value?.request_id && Array.isArray(value.selected_assets) ? value : null } catch { return null }
  }
  const [pending, setPending] = useState(readPending), [busy, setBusy] = useState(false), [error, setError] = useState('')
  useEffect(() => { setPending(readPending()); setBusy(false); setError('') }, [key])
  const submit = async () => {
    if (busy || disabled && !pending) return
    const command: Intent = readPending() || { request_id: requestId(), session_request_id: requestId(), session_id: sessionId || null, selected_assets: [...selectedAssets], ...(linkId ? { link_id: linkId } : {}), ...(scopeRevision !== undefined ? { expected_scope_revision: scopeRevision } : {}), ...(parameters ? { parameters } : {}) }
    const retain = () => { localStorage.setItem(key, JSON.stringify(command)); if (live()) setPending({ ...command }) }
    retain(); setBusy(true); setError('')
    try {
      if (!command.session_id) {
        const session = await agentWrite<AgentSession>(`/workspaces/${encodeURIComponent(workspace)}/agent/sessions`, {
          name: action === 'pdf-text' ? '提取 PDF 指定页文字' : '提取图片文字', selected_assets: command.selected_assets, request_id: command.session_request_id,
        })
        command.session_id = session.id; retain()
      }
      await agentWrite(`/agent/sessions/${encodeURIComponent(command.session_id)}/direct/${action}`, {
        request_id: command.request_id, ...(command.link_id ? { link_id: command.link_id } : {}),
        ...(command.expected_scope_revision !== undefined ? { expected_scope_revision: command.expected_scope_revision } : {}),
        ...command.parameters,
      })
      localStorage.removeItem(key); if (live()) { setPending(null); onAccepted(command.session_id) }
    } catch (caught) {
      // Only a definite rejection permits a fresh identity. Lost responses stay recoverable.
      if (caught instanceof AgentRequestError && [400, 403, 404, 409, 422].includes(caught.status)) { localStorage.removeItem(key); if (live()) setPending(null) }
      if (live()) setError((caught as Error).message)
    } finally { if (live()) setBusy(false) }
  }
  return <Space orientation="vertical" size="small">
    <Button type={buttonType} loading={busy} disabled={busy || !pending && (disabled || !selectedAssets.length)} onClick={() => { void submit() }}>{pending ? '核实原文字提取请求' : action === 'pdf-text' ? '提取指定页文字' : '提取所选图片文字'}</Button>
    {pending && !busy && <span role="status">上次请求待核实，继续读取原操作回执。</span>}
    {error && <Alert type="error" title={error} />}
  </Space>
}
