import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Alert, Button, Space, Table, Tag, Typography } from 'antd'
import type { UploadBinding, UploadReceipt, UploadStatus } from '../../../../../packages/contracts/types.generated'
import { HttpError, newRequestId, request } from './api'

const { Text, Paragraph } = Typography
type UploadState = 'queued' | 'sending' | 'uncertain' | 'failed' | 'committed'
type QueueItem = { binding: UploadBinding; state: UploadState; message?: string; asset_id?: string; receipt?: UploadReceipt }
const labels: Record<UploadState, string> = { queued: '未提交', sending: '上传中', uncertain: '待核实', failed: '失败', committed: '成功' }
const key = (workspaceId: string) => `clear-upload-queue:${workspaceId}`
function restored(workspaceId: string): QueueItem[] {
  try {
    const rows = JSON.parse(localStorage.getItem(key(workspaceId)) || '[]')
    if (!Array.isArray(rows)) return []
    return rows.filter((row): row is QueueItem => row?.binding?.workspace_id === workspaceId &&
      /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(row.binding.request_id) && typeof row.binding.name === 'string' &&
      /^[a-f0-9]{64}$/.test(row.binding.sha256) && Number.isSafeInteger(row.binding.size_bytes) && row.binding.size_bytes > 0 &&
      ['queued', 'sending', 'uncertain', 'failed', 'committed'].includes(row.state))
      .map(row => row.state === 'sending' ? { ...row, state: 'uncertain', message: '上次响应需要核实' } : row)
  } catch { return [] }
}
async function bytes(file: File): Promise<ArrayBuffer> {
  if (file.arrayBuffer) return file.arrayBuffer()
  return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result as ArrayBuffer); reader.onerror = () => reject(new Error('文件读取失败')); reader.readAsArrayBuffer(file) })
}
export async function fileSha256(file: File) {
  const digest = await crypto.subtle.digest('SHA-256', await bytes(file))
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
}
function matches(left: UploadBinding, right: UploadBinding) {
  return left.workspace_id === right.workspace_id && left.request_id === right.request_id && left.name === right.name && left.size_bytes === right.size_bytes && left.sha256 === right.sha256
}
export function UploadQueue({ workspaceId, disabled = false, onBusy, onUploaded, compact = false }: { workspaceId: string; disabled?: boolean; onBusy?: (value: boolean) => void; onUploaded?: (assetId: string) => void; compact?: boolean }) {
  const client = useQueryClient()
  const [rows, setRows] = useState(() => restored(workspaceId))
  const current = useRef(rows)
  const files = useRef(new Map<string, File>())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const remember = (value: QueueItem[]) => { localStorage.setItem(key(workspaceId), JSON.stringify(value)); current.current = value; setRows(value) }
  const update = (id: string, value: Partial<QueueItem>) => remember(current.current.map(row => row.binding.request_id === id ? { ...row, ...value } : row))
  const loading = (value: boolean) => { setBusy(value); onBusy?.(value) }
  const path = (binding: UploadBinding) => `/workspaces/${encodeURIComponent(workspaceId)}/assets/uploads/${encodeURIComponent(binding.request_id)}`
  const refreshAssets = () => Promise.all([client.invalidateQueries({ queryKey: ['batch-assets', workspaceId] }), client.invalidateQueries({ queryKey: ['assets', workspaceId] })])
  function committed(status: UploadStatus, binding: UploadBinding) {
    if (!matches(status.binding, binding)) throw new Error('上传响应身份不一致，保留原请求等待核实')
    if (status.status !== 'committed') return false
    const receipt = status.receipt
    if (!receipt || !matches(receipt.binding, binding) || receipt.asset.workspace_id !== workspaceId || receipt.asset.blob_hash !== binding.sha256 || receipt.asset.size_bytes !== binding.size_bytes || receipt.asset.original_name !== binding.name) throw new Error('上传收据与原文件不一致，保留原请求等待核实')
    update(binding.request_id, { state: 'committed', asset_id: receipt.asset.asset_id, receipt, message: undefined })
    onUploaded?.(receipt.asset.asset_id)
    return true
  }
  async function add(selected: File[]) {
    if (busy || disabled) return
    loading(true); setError('')
    try {
      for (const file of selected) {
        if (!file.size || file.size > 128 * 1024 * 1024) throw new Error(`${file.name}：请选择非空且不超过 128 MB 的文件`)
        const sha256 = await fileSha256(file)
        const sameName = current.current.find(row => row.binding.name === file.name)
        if (sameName) {
          if (sameName.binding.size_bytes !== file.size || sameName.binding.sha256 !== sha256) throw new Error(`${file.name}：名称相同但大小或 SHA256 不同，不能替换原恢复请求`)
          files.current.set(sameName.binding.request_id, file)
        } else {
          const binding: UploadBinding = { schema_version: '1.0', workspace_id: workspaceId, request_id: newRequestId(), name: file.name, size_bytes: file.size, sha256 }
          files.current.set(binding.request_id, file)
          remember([...current.current, { binding, state: 'queued' }])
        }
      }
    } catch (failure) { setError((failure as Error).message) }
    finally { loading(false) }
  }
  async function upload() {
    if (busy || disabled) return
    loading(true); setError('')
    try {
      for (const original of [...current.current]) {
        if (original.state === 'committed') continue
        const binding = original.binding
        try {
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
          const status = await request<UploadStatus>(`${path(binding)}?${query}`, { method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: file })
          if (!committed(status, binding)) throw new Error(status.error?.message || '上传尚未完成，请核实同一请求')
          await refreshAssets()
        } catch (failure) {
          // A lost response may already have registered the asset. Query before presenting a retry.
          try {
            const status = await request<UploadStatus>(path(binding))
            if (committed(status, binding)) { await refreshAssets(); continue }
            update(binding.request_id, { state: status.status === 'rejected' ? 'failed' : 'uncertain', message: status.error?.message || (failure as Error).message })
          } catch (verification) {
            update(binding.request_id, { state: verification instanceof HttpError && verification.status === 404 ? 'failed' : 'uncertain', message: (failure as Error).message })
          }
          break
        }
      }
    } finally { loading(false) }
  }
  const success = rows.filter(row => row.state === 'committed').length
  const failed = rows.filter(row => row.state === 'failed' || row.state === 'uncertain').length
  const waiting = rows.filter(row => row.state === 'queued').length
  return <div className="batch-upload-queue"><Space wrap><label className="batch-upload">{busy ? '核实 / 上传资料中…' : rows.some(row => row.state !== 'committed') ? '添加或重选原文件' : '添加新资料'}
    <input aria-label={compact ? '添加审计资料' : '添加批次资料'} type="file" multiple disabled={disabled || busy} onChange={event => { const selected = Array.from(event.target.files || []); event.target.value = ''; if (selected.length) void add(selected) }} /></label>
    <Button disabled={disabled || busy || !rows.some(row => row.state !== 'committed')} onClick={() => void upload()}>{failed ? '核实并恢复失败与未提交' : '上传未提交资料'}</Button>
    {!!rows.length && <Text>成功 {success} · 失败 / 待核实 {failed} · 未提交 {waiting}</Text>}
    {!!rows.length && success === rows.length && <Button disabled={busy || disabled} onClick={() => { remember([]); files.current.clear() }}>清除已完成上传记录</Button>}</Space>
    {error && <Alert type="error" title={error} />}
    {!!rows.length && <><Table<QueueItem> size="small" rowKey={row => row.binding.request_id} dataSource={rows} pagination={false}
      columns={[{ title: '文件', key: 'name', render: (_, row) => row.binding.name }, { title: '大小', key: 'size', render: (_, row) => `${row.binding.size_bytes.toLocaleString()} 字节` },
        { title: '上传状态', key: 'state', render: (_, row) => <Tag color={row.state === 'committed' ? 'success' : row.state === 'failed' ? 'error' : 'default'}>{labels[row.state]}</Tag> },
        { title: '恢复说明', dataIndex: 'message' }]} />
      <Paragraph type="secondary">{compact ? '上传成功的资料可直接加入当前任务。上传中断后可在这里继续。' : '成功项保留；发生问题时后续文件暂不提交。页面重载后，原文件需重新选择，名称、大小和 SHA256 必须一致。恢复先核实原请求，再只继续必要文件。'}</Paragraph></>}
  </div>
}
