import { useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Space, Table, Tag, Typography } from 'antd'
import type { UploadBinding, UploadReceipt, UploadStatus } from '../../../../../packages/contracts/types.generated'
import { HttpError, newRequestId, request } from './api'
import { AUDIO_CHUNK_BYTES, chunkSha256, fileSha256 } from './fileHash'
export { fileSha256 } from './fileHash'

const { Text, Paragraph } = Typography
type UploadState = 'queued' | 'sending' | 'uncertain' | 'failed' | 'committed' | 'cancelled'
type QueueItem = { binding: UploadBinding; state: UploadState; message?: string; asset_id?: string; receipt?: UploadReceipt; mode?: 'audio_chunks'; offset?: number }
type AudioStatus = Omit<UploadStatus, 'status'> & { status: 'uploading' | 'committed' | 'cancelled' | 'rejected'; next_offset: number; chunk_bytes: number }
const labels: Record<UploadState, string> = { queued: '未提交', sending: '上传中', uncertain: '待核实', failed: '失败', committed: '成功', cancelled: '已取消' }
const AUDIO_MAX_BYTES = 4 * 1024 * 1024 * 1024
const LEGACY_MAX_BYTES = 128 * 1024 * 1024
const audioName = (name: string) => /\.(wav|mp3|m4a|flac|ogg|aac|wma)$/i.test(name)
const unfinished = (row: QueueItem) => !['committed', 'cancelled'].includes(row.state)
const key = (workspaceId: string) => `clear-upload-queue:${workspaceId}`
function restored(workspaceId: string): QueueItem[] {
  try {
    const rows = JSON.parse(localStorage.getItem(key(workspaceId)) || '[]')
    if (!Array.isArray(rows)) return []
    return rows.filter((row): row is QueueItem => row?.binding?.workspace_id === workspaceId &&
      /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(row.binding.request_id) && typeof row.binding.name === 'string' &&
      /^[a-f0-9]{64}$/.test(row.binding.sha256) && Number.isSafeInteger(row.binding.size_bytes) && row.binding.size_bytes > 0 &&
      row.binding.size_bytes <= (row.mode === 'audio_chunks' && audioName(row.binding.name) ? AUDIO_MAX_BYTES : LEGACY_MAX_BYTES) &&
      ['queued', 'sending', 'uncertain', 'failed', 'committed', 'cancelled'].includes(row.state))
      .map(row => row.state === 'sending' ? { ...row, state: 'uncertain', message: '上次响应需要核实' } : row)
  } catch { return [] }
}
function matches(left: UploadBinding, right: UploadBinding) {
  return left.workspace_id === right.workspace_id && left.request_id === right.request_id && left.name === right.name && left.size_bytes === right.size_bytes && left.sha256 === right.sha256
}
export function UploadQueue({ workspaceId, disabled = false, onBusy, onUploaded, compact = false, allowLargeAudio = false }: { workspaceId: string; disabled?: boolean; onBusy?: (value: boolean) => void; onUploaded?: (assetId: string) => void; compact?: boolean; allowLargeAudio?: boolean }) {
  const client = useQueryClient()
  const [rows, setRows] = useState(() => restored(workspaceId))
  const current = useRef(rows)
  const files = useRef(new Map<string, File>())
  const active = useRef<AbortController | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [hashProgress, setHashProgress] = useState('')
  useEffect(() => () => active.current?.abort(new DOMException('页面已关闭', 'AbortError')), [])
  const remember = (value: QueueItem[]) => { localStorage.setItem(key(workspaceId), JSON.stringify(value)); current.current = value; setRows(value) }
  const update = (id: string, value: Partial<QueueItem>) => remember(current.current.map(row => row.binding.request_id === id ? { ...row, ...value } : row))
  const loading = (value: boolean) => { setBusy(value); onBusy?.(value) }
  const path = (binding: UploadBinding, audio = false) => `/workspaces/${encodeURIComponent(workspaceId)}/assets/${audio ? 'audio-uploads' : 'uploads'}/${encodeURIComponent(binding.request_id)}`
  const refreshAssets = () => Promise.all([client.invalidateQueries({ queryKey: ['batch-assets', workspaceId] }), client.invalidateQueries({ queryKey: ['assets', workspaceId] })])
  function committed(status: UploadStatus | AudioStatus, binding: UploadBinding) {
    if (!matches(status.binding, binding)) throw new Error('上传响应身份不一致，保留原请求等待核实')
    if (status.status !== 'committed') return false
    const receipt = status.receipt
    if (!receipt || !matches(receipt.binding, binding) || receipt.asset.workspace_id !== workspaceId || receipt.asset.blob_hash !== binding.sha256 || receipt.asset.size_bytes !== binding.size_bytes || receipt.asset.original_name !== binding.name) throw new Error('上传收据与原文件不一致，保留原请求等待核实')
    update(binding.request_id, { state: 'committed', asset_id: receipt.asset.asset_id, receipt, message: undefined })
    onUploaded?.(receipt.asset.asset_id)
    return true
  }
  function audioStatus(status: AudioStatus, binding: UploadBinding) {
    if (!matches(status.binding, binding) || status.chunk_bytes !== AUDIO_CHUNK_BYTES || !Number.isSafeInteger(status.next_offset) || status.next_offset < 0 || status.next_offset > binding.size_bytes || (status.next_offset !== binding.size_bytes && status.next_offset % AUDIO_CHUNK_BYTES !== 0)) throw new Error('分块响应身份或位置不一致，请保留原请求核实')
    if (committed(status, binding)) return status
    if (status.status === 'cancelled') { update(binding.request_id, { state: 'cancelled', message: '上传已取消，原件未登记' }); return status }
    if (status.status !== 'uploading') throw new Error(status.error?.message || '原上传内容校验失败，请取消后重新添加原文件')
    update(binding.request_id, { offset: status.next_offset })
    return status
  }
  async function audioMutation(binding: UploadBinding, suffix: string, init: RequestInit) {
    try { return audioStatus(await request<AudioStatus>(path(binding, true) + suffix, init), binding) }
    catch (failure) {
      // No mutation is repeated after a lost reply: reconcile its durable offset.
      const status = audioStatus(await request<AudioStatus>(path(binding, true)), binding)
      if (status.status === 'uploading' && suffix.startsWith('/chunks') && status.next_offset === Number(new URLSearchParams(suffix.split('?')[1]).get('offset'))) throw failure
      return status
    }
  }
  async function uploadAudio(original: QueueItem, signal: AbortSignal) {
    const binding = original.binding
    if (!allowLargeAudio) throw new Error('请在音频工作台恢复此音频分块上传')
    let status: AudioStatus
    try { status = audioStatus(await request<AudioStatus>(path(binding, true), { signal }), binding) }
    catch (failure) {
      if (!(failure instanceof HttpError) || failure.status !== 404) throw failure
      status = await audioMutation(binding, '', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(binding), signal })
    }
    if (status.status !== 'uploading') return
    const file = files.current.get(binding.request_id)
    if (!file) throw new Error(`${binding.name}：重选原文件后继续，必须匹配名称、大小和 SHA256`)
    update(binding.request_id, { state: 'sending', message: undefined })
    while (status.next_offset < binding.size_bytes) {
      signal.throwIfAborted()
      const offset = status.next_offset
      const chunk = file.slice(offset, Math.min(offset + AUDIO_CHUNK_BYTES, binding.size_bytes))
      const sha256 = await chunkSha256(chunk)
      signal.throwIfAborted()
      status = await audioMutation(binding, `/chunks?${new URLSearchParams({ offset: String(offset), sha256 })}`, {
        method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: chunk, signal,
      })
      if (status.status !== 'uploading') return
      if (status.next_offset <= offset) throw new Error('分块尚未确认，请先核实后恢复')
    }
    signal.throwIfAborted()
    update(binding.request_id, { message: '上传完成，正在校验完整文件…' })
    status = await audioMutation(binding, '/complete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}', signal })
    if (status.status === 'uploading') throw new Error('完整文件校验尚需核实，请恢复原请求')
  }
  async function add(selected: File[]) {
    if (busy || disabled) return
    loading(true); setError('')
    try {
      for (const file of selected) {
        const chunked = allowLargeAudio && audioName(file.name)
        if (!file.size || file.size > (chunked ? AUDIO_MAX_BYTES : LEGACY_MAX_BYTES)) throw new Error(`${file.name}：请选择非空且不超过 ${chunked ? '4 GiB' : '128 MB'} 的${chunked ? '音频' : ''}文件`)
        const controller = new AbortController(); active.current = controller
        const sha256 = await fileSha256(file, (done, total) => setHashProgress(`正在校验 ${file.name} · ${Math.floor(done / total * 100)}%`), controller.signal)
        const sameName = current.current.find(row => row.binding.name === file.name && row.state !== 'cancelled')
        if (sameName) {
          if (sameName.binding.size_bytes !== file.size || sameName.binding.sha256 !== sha256) throw new Error(`${file.name}：名称相同但大小或 SHA256 不同，不能替换原恢复请求`)
          files.current.set(sameName.binding.request_id, file)
        } else {
          const binding: UploadBinding = { schema_version: '1.0', workspace_id: workspaceId, request_id: newRequestId(), name: file.name, size_bytes: file.size, sha256 }
          files.current.set(binding.request_id, file)
          remember([...current.current, { binding, state: 'queued', ...(chunked ? { mode: 'audio_chunks' as const, offset: 0 } : {}) }])
        }
      }
    } catch (failure) { setError((failure as Error).message) }
    finally { active.current = null; setHashProgress(''); loading(false) }
  }
  async function upload() {
    if (busy || disabled) return
    loading(true); setError('')
    try {
      for (const original of [...current.current]) {
        if (!unfinished(original)) continue
        const binding = original.binding
        const controller = new AbortController(); active.current = controller
        try {
          if (original.mode === 'audio_chunks') { await uploadAudio(original, controller.signal); await refreshAssets(); if (controller.signal.aborted) break; continue }
          if (original.state !== 'queued') {
            try {
              const status = await request<UploadStatus>(path(binding))
              if (committed(status, binding)) { await refreshAssets(); continue }
              if (!matches(status.binding, binding)) throw new Error('服务保存的原请求元数据不一致，不能重复提交')
              if (status.status === 'rejected' && status.error?.details.effect !== 'no_registration') throw new Error('服务尚未证明未登记，请继续核实原请求')
            } catch (failure) { if (!(failure instanceof HttpError) || failure.status !== 404) throw failure }
          }
          const file = files.current.get(binding.request_id)
          if (!file) throw new Error(`${binding.name}：重选原文件后继续，必须匹配名称、大小和 SHA256`)
          update(binding.request_id, { state: 'sending', message: undefined })
          const query = new URLSearchParams({ name: binding.name, sha256: binding.sha256, size_bytes: String(binding.size_bytes) })
          const status = await request<UploadStatus>(`${path(binding)}?${query}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file, signal: controller.signal })
          if (!committed(status, binding)) throw new Error(status.error?.message || '上传尚未完成，请核实同一请求')
          await refreshAssets()
        } catch (failure) {
          // A lost response may already have registered the asset. Query before presenting a retry.
          try {
            const status = await request<UploadStatus | AudioStatus>(path(binding, original.mode === 'audio_chunks'))
            if (committed(status, binding)) { await refreshAssets(); if (controller.signal.aborted) break; continue }
            if (status.status === 'cancelled') update(binding.request_id, { state: 'cancelled', message: '上传已取消' })
            else update(binding.request_id, { state: status.status === 'rejected' ? 'failed' : 'uncertain', ...('next_offset' in status ? { offset: status.next_offset } : {}), message: controller.signal.aborted ? '已暂停；恢复时先核实已确认位置' : status.error?.message || (failure as Error).message })
          } catch (verification) {
            update(binding.request_id, { state: verification instanceof HttpError && verification.status === 404 ? 'failed' : 'uncertain', message: (failure as Error).message })
          }
          break
        }
      }
    } finally { active.current = null; loading(false) }
  }
  async function cancel(row: QueueItem) {
    if (busy || disabled) return
    loading(true); setError('')
    try {
      if (row.state === 'queued' && !row.offset) {
        try { await request<AudioStatus>(path(row.binding, true)) }
        catch (failure) {
          if (!(failure instanceof HttpError) || failure.status !== 404) throw failure
          update(row.binding.request_id, { state: 'cancelled', message: '上传已取消，未提交文件' }); files.current.delete(row.binding.request_id); return
        }
      }
      const status = await audioMutation(row.binding, '/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      if (status.status === 'uploading') throw new Error('取消响应尚需核实，请重试取消原请求')
      files.current.delete(row.binding.request_id)
    } catch (failure) { update(row.binding.request_id, { state: 'uncertain', message: (failure as Error).message }) }
    finally { loading(false) }
  }
  const success = rows.filter(row => row.state === 'committed').length
  const failed = rows.filter(row => row.state === 'failed' || row.state === 'uncertain').length
  const waiting = rows.filter(row => row.state === 'queued').length
  return <div className="batch-upload-queue"><Space wrap><label className="batch-upload">{busy ? '核实 / 上传资料中…' : rows.some(unfinished) ? '添加或重选原文件' : '添加新资料'}
    <input aria-label={compact ? '添加审计资料' : '添加批次资料'} type="file" multiple disabled={disabled || busy} onChange={event => { const selected = Array.from(event.target.files || []); event.target.value = ''; if (selected.length) void add(selected) }} /></label>
    <Button disabled={disabled || busy || !rows.some(unfinished)} onClick={() => void upload()}>{failed ? '核实并恢复失败与未提交' : '上传未提交资料'}</Button>
    {busy && <Button onClick={() => active.current?.abort(new DOMException('已暂停', 'AbortError'))}>暂停上传 / 校验</Button>}
    {!!rows.length && <Text>成功 {success} · 失败 / 待核实 {failed} · 未提交 {waiting}</Text>}
    {!!rows.length && !rows.some(unfinished) && <Button disabled={busy || disabled} onClick={() => { remember([]); files.current.clear() }}>清除已完成上传记录</Button>}</Space>
    {hashProgress && <Text role="status">{hashProgress}</Text>}
    {error && <Alert type="error" title={error} />}
    {!!rows.length && <><Table<QueueItem> size="small" rowKey={row => row.binding.request_id} dataSource={rows} pagination={false}
      columns={[{ title: '文件', key: 'name', render: (_, row) => row.binding.name }, { title: '大小', key: 'size', render: (_, row) => `${row.binding.size_bytes.toLocaleString()} 字节` },
        { title: '上传状态', key: 'state', render: (_, row) => <Tag color={row.state === 'committed' ? 'success' : row.state === 'failed' ? 'error' : 'default'}>{labels[row.state]}</Tag> },
        { title: '恢复说明', key: 'recovery', render: (_, row) => <Space wrap>{row.mode === 'audio_chunks' && <Text>{Math.floor((row.offset || 0) / row.binding.size_bytes * 100)}% 已确认</Text>}{row.message}{row.mode === 'audio_chunks' && unfinished(row) && <Button size="small" disabled={busy || disabled} onClick={() => void cancel(row)}>取消此上传</Button>}</Space> }]} />
      {allowLargeAudio && <Paragraph type="secondary">音频支持最多 4 GiB，并可断点续传；其他资料仍限 128 MB。暂停保留进度，取消释放临时文件。</Paragraph>}
      <Paragraph type="secondary">{compact ? '上传成功的资料可直接加入当前任务。上传中断后可在这里继续。' : '成功项保留；发生问题时后续文件暂不提交。页面重载后，原文件需重新选择，名称、大小和 SHA256 必须一致。恢复先核实原请求，再只继续必要文件。'}</Paragraph></>}
  </div>
}
