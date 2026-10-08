import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Alert, Button, InputNumber, Space, Tag, Typography } from 'antd'
import { reliableDiagnosticBox, type Diagnostic } from './screenDiagnosticWorkflow'

type Target = { input_version_id: string; result_id: string; result_revision: number; issue_ids: string[] }
type Submission = { request_id: string; budget_seconds: number; target: Target }
type Command = { action: string; request_id: string }
type Candidate = { task_id: string; status: string; candidate_state: string; current_binding: boolean; text: string | null; text_preview_only: boolean; content_status?: string | null; binding: { result_id: string; result_revision: number }; artifact?: Record<string, { sha256: string }> | null }
type Receipt = { request_id: string; status: string; execution_status: string; reason?: string; owned_task_ids: string[]; items: Candidate[]; values_modified: false; selection_applied: false; human_review: string }
const id = () => 'r' + crypto.randomUUID().replaceAll('-', '')
async function json<T>(url: string, body?: unknown): Promise<T> {
  const response = await fetch(url, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined)
  if (!response.ok) throw new Error(response.status === 409 ? '原图、结果或原请求已变化，请重新核对' : '候选回执暂不可读，请沿原请求重试')
  return response.json() as Promise<T>
}
function remembered(key: string): Submission | null {
  try { return JSON.parse(sessionStorage.getItem(key) || 'null') as Submission | null } catch { return null }
}
function rememberedControl(key: string, submission: Submission | null): Command | null {
  try {
    const saved = JSON.parse(sessionStorage.getItem(key) || 'null') as { submission_request_id: string; command: Command } | null
    return saved?.submission_request_id === submission?.request_id ? saved?.command ?? null : null
  } catch { return null }
}

export function LocalCandidates({ workspaceId, batchId, inputVersionId, resultId, revision, diagnostic, remainingBudget, primaryPending, currentValue }: {
  workspaceId: string; batchId: string; inputVersionId: string; resultId?: string; revision?: number; diagnostic?: Diagnostic; remainingBudget: number | null; primaryPending: boolean; currentValue?: string
}) {
  const client = useQueryClient()
  const storageKey = `clear-local-candidate:${workspaceId}:${batchId}:${inputVersionId}`
  const [submission, setSubmission] = useState<Submission | null>(() => remembered(storageKey))
  const [budget, setBudget] = useState(Math.max(10, Math.min(60, remainingBudget ?? 60)))
  const controlKey = storageKey + ':control'
  const [controlIntent, setControlIntent] = useState<Command | null>(() => rememberedControl(controlKey, submission))
  const base = `/api/platform/v1/workspaces/${encodeURIComponent(workspaceId)}/ocr/screen-batches/${encodeURIComponent(batchId)}/local-candidates`
  const queryKey = ['ocr-local-candidate', workspaceId, batchId, submission?.request_id]
  const receipt = useQuery({ queryKey, enabled: !!submission, retry: false,
    queryFn: () => json<Receipt>(`${base}/${submission!.request_id}?offset=0&limit=12`),
    refetchInterval: q => q.state.data?.execution_status === 'pending' ? 2500 : false })
  const start = useMutation({ mutationFn: (request: Submission) => json<Receipt>(base, request),
    onSuccess: value => client.setQueryData(['ocr-local-candidate', workspaceId, batchId, value.request_id], value) })
  const control = useMutation({ mutationFn: (command: Command) => json<Receipt>(`${base}/${submission!.request_id}/control`, command),
    onSuccess: value => { setControlIntent(null); sessionStorage.removeItem(controlKey); client.setQueryData(['ocr-local-candidate', workspaceId, batchId, value.request_id], value) } })
  const reliable = diagnostic?.location.level === 'cell' && !!reliableDiagnosticBox(diagnostic)
  const supported = reliable && !!resultId && revision !== undefined && remainingBudget !== null && remainingBudget >= 10 && !primaryPending
  const pending = receipt.data?.execution_status === 'pending'
  const canControl = (receipt.data?.owned_task_ids?.length ?? 0) > 0
  const canStart = supported && !start.isPending && !pending && !controlIntent
  const waitingUnknown = !!submission && !receipt.data
  function generate() {
    if (!canStart || !diagnostic || !resultId || revision === undefined) return
    const request = { request_id: id(), budget_seconds: Math.min(budget, Math.floor(remainingBudget!)),
      target: { input_version_id: inputVersionId, result_id: resultId, result_revision: revision, issue_ids: [diagnostic.id] } }
    sessionStorage.setItem(storageKey, JSON.stringify(request)); setSubmission(request); start.mutate(request)
  }
  function command(action: string) {
    const request = controlIntent || { action, request_id: id() }
    if (request.action !== action) return
    sessionStorage.setItem(controlKey, JSON.stringify({ submission_request_id: submission!.request_id, command: request }))
    setControlIntent(request); control.mutate(request)
  }
  return <section aria-label="局部文字候选">
    <Typography.Title level={5}>局部文字候选</Typography.Title>
    <Typography.Paragraph type="secondary">对有可靠单格定位的疑点生成文字候选，保留原值。候选需核对原图。</Typography.Paragraph>
    <Space wrap><span>本次预算</span><InputNumber aria-label="局部候选预算秒" min={10} max={Math.min(900, Math.max(10, remainingBudget ?? 900))} value={budget} onChange={value => value !== null && setBudget(value)} /><span>秒</span>
      <Button disabled={!canStart || waitingUnknown} loading={start.isPending} onClick={generate}>生成此格文字候选</Button>
    </Space>
    {!supported && <p>{primaryPending ? '等待整批完成后再处理局部疑点。' : !reliable ? '此疑点没有可靠单格定位，请核对原图或整图候选。' : remainingBudget === null ? '剩余预算尚未核实。' : '剩余预算不足或结果版本尚未核实。'}</p>}
    {(receipt.error || start.error || control.error) && <Alert type="warning" title={(receipt.error || start.error || control.error)?.message} />}
    {waitingUnknown && submission && <Button loading={start.isPending} onClick={() => start.mutate(submission)}>核实原候选请求</Button>}
    {controlIntent && <Button loading={control.isPending} onClick={() => control.mutate(controlIntent)}>核实原控制操作</Button>}
    {receipt.data && <><p>处理状态：{pending ? '后台处理中' : receipt.data.status === 'blocked' ? '本次未新增识别' : '已结束'}{receipt.data.reason === 'lineage_budget_exhausted' ? ' · 剩余预算不足' : ''}</p>
      {receipt.data.status === 'reused' && !canControl && <p>已复用此前相同位置的识别，任务由原请求控制。</p>}
      {pending && canControl && <Space wrap>{(['pause', 'resume', 'cancel'] as const).map(action => <Button key={action} disabled={control.isPending || (!!controlIntent && controlIntent.action !== action)} onClick={() => command(action)}>{({ pause: '暂停候选', resume: '继续候选', cancel: '取消候选' })[action]}</Button>)}</Space>}
      {canControl && receipt.data.items.some(item => item.status === 'failed') && <Button disabled={control.isPending || (!!controlIntent && controlIntent.action !== 'retry_failed')} onClick={() => command('retry_failed')}>仅重试失败候选</Button>}
      {receipt.data.items.map(item => {
        const stale = !item.current_binding || item.candidate_state === 'stale' || item.binding.result_id !== resultId || item.binding.result_revision !== revision
        const fileBase = `${base}/${receipt.data!.request_id}/tasks/${item.task_id}/files`
        return <div key={item.task_id} style={{ marginTop: 12 }}><Tag>{stale ? '历史候选，所依据的结果已变化' : item.status === 'succeeded' ? '候选待核对' : item.status === 'failed' ? '候选失败，原值保留' : '等待候选'}</Tag>
          <p>当前格值：{currentValue ?? '请在表格中核对当前值'}</p><p>候选文字：{item.text === '' ? '识别为空，请核对原图' : item.text ?? '尚无可用文字'}{item.text_preview_only ? '（预览已截断，请查看完整证据）' : ''}</p>
          {item.status === 'succeeded' && <img alt="局部候选所用原图裁剪" src={`${fileBase}/crop.png`} style={{ maxWidth: '100%', maxHeight: 220 }} />}
          <Space wrap>{Object.keys(item.artifact || {}).map(file => <a key={file} href={`${fileBase}/${encodeURIComponent(file)}`} target="_blank" rel="noreferrer">{({ 'raw.json': '原始识别证据', 'result.txt': '完整候选文字', 'result.json': '候选结果证据', 'error.json': '失败原因记录', 'input.png': '实际识别图片', 'result.md': '候选文字文档' } as Record<string, string>)[file] || '候选证据'}</a>)}</Space>
        </div>
      })}</>}
  </section>
}
