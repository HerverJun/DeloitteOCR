import { useState } from 'react'
import { Alert, Button, Space, Typography } from 'antd'
import { agentRequest, type AgentRef } from './api'

export type ReadableTextResult = { result_id?: string; revision?: string | number; reference?: AgentRef; text: string; text_total_chars?: number; text_offset?: number; text_next_offset?: number | null; text_truncated?: boolean; text_sha256?: string; download_url?: string; source_locator?: { page_number?: number } }
const safeDownload = (value?: string) => value && (/^\/(?!\/)/.test(value) || /^https?:\/\//i.test(value)) ? value : undefined

/** Keep OCR as literal source text; markdown interpretation would change the user's copy. */
export function ReadableText({ result, taskId }: { result: ReadableTextResult; taskId?: string }) {
  const [pages, setPages] = useState([result]), [page, setPage] = useState(0), [busy, setBusy] = useState(false), [copied, setCopied] = useState(false), [error, setError] = useState('')
  const current = pages[page], nextOffset = current.text_next_offset
  const total = result.text_total_chars ?? Array.from(result.text).length
  const read = async (offset: number) => {
    if (!taskId || !result.reference) throw new Error('全文分页暂不可读取，请使用 TXT 下载。')
    const value = await agentRequest<ReadableTextResult>(`/agent/sessions/${encodeURIComponent(taskId)}/results/text?reference=${encodeURIComponent(JSON.stringify(result.reference))}&offset=${offset}&limit=8000`)
    if (typeof value.text !== 'string' || value.text_offset !== offset || value.text_next_offset != null && value.text_next_offset <= offset || result.text_sha256 && value.text_sha256 !== result.text_sha256) throw new Error('全文分页未能核对同一结果版本，请重新读取。')
    return value
  }
  const next = async () => {
    if (nextOffset == null) return
    if (pages[page + 1]) { setPage(page + 1); return }
    setBusy(true); setError('')
    try { const value = await read(nextOffset); setPages(old => [...old, value]); setPage(page + 1) }
    catch (caught) { setError((caught as Error).message) }
    finally { setBusy(false) }
  }
  const copy = async () => {
    setBusy(true); setError(''); setCopied(false)
    try {
      if (!navigator.clipboard) throw new Error('暂不能复制，可选中文字或下载 TXT。')
      const all = [...pages]
      while (all.at(-1)?.text_next_offset != null) all.push(await read(all.at(-1)!.text_next_offset!))
      const text = all.map(value => value.text).join('')
      if (Array.from(text).length !== total || all[0].text_offset && all[0].text_offset !== 0 || result.text_truncated && result.text_next_offset == null) throw new Error('全文尚未完整读取，请使用 TXT 下载并核对。')
      setPages(all); await navigator.clipboard.writeText(text); setCopied(true)
    } catch (caught) { setError((caught as Error).message) }
    finally { setBusy(false) }
  }
  return <section className="agent-readable-text" aria-label="识别全文">
    <Space wrap><Typography.Text strong>识别原文{result.source_locator?.page_number ? ` · 第 ${result.source_locator.page_number} 页` : ''} · {total.toLocaleString('zh-CN')} 字符</Typography.Text>
      <Button size="small" loading={busy} onClick={() => { void copy() }}>{copied ? '已复制全文' : '复制全文'}</Button>
      {safeDownload(result.download_url) && <Button size="small" href={safeDownload(result.download_url)}>下载 TXT</Button>}
    </Space>
    <Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{current.text || '识别结果没有正文。'}</Typography.Paragraph>
    {(result.text_truncated || pages.length > 1) && <Space wrap><Typography.Text type="secondary">第 {page + 1} 段 · 全文 {total.toLocaleString('zh-CN')} 字符{nextOffset != null ? '，后续内容可继续读取' : ''}</Typography.Text>
      <Button size="small" disabled={!page || busy} onClick={() => setPage(page - 1)}>上一段</Button><Button size="small" disabled={nextOffset == null || busy} onClick={() => { void next() }}>下一段</Button></Space>}
    {error && <Alert type="warning" title={error} />}
  </section>
}
