import { Children, isValidElement, memo, useState, type ReactNode } from 'react'
import { Alert, Button, Tag, Typography } from 'antd'
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { ReferenceLinks, statusLabels } from './TaskPanel'
import type { MessageSegment } from './conversationProjection'
import type { AgentRef } from './api'
import { ReadableText, type ReadableTextResult } from './ReadableText'

function nodeText(node: ReactNode): string {
  return Children.toArray(node).map(child => typeof child === 'string' || typeof child === 'number' ? String(child) : isValidElement<{ children?: ReactNode }>(child) ? nodeText(child.props.children) : '').join('')
}
function CodeBlock({ children }: { children?: ReactNode }) {
  const [copied, setCopied] = useState(false), [failed, setFailed] = useState(false)
  return <div className="agent-code-block"><Button size="small" aria-label="复制代码" onClick={() => {
    void navigator.clipboard?.writeText(nodeText(children)).then(() => { setCopied(true); setFailed(false) }).catch(() => setFailed(true))
    if (!navigator.clipboard) setFailed(true)
  }}>{copied ? '已复制' : '复制代码'}</Button><pre>{children}</pre>{failed && <span role="status">暂不能复制，可选中文字复制。</span>}</div>
}
export function AgentMarkdown({ content }: { content: string }) {
  return <div className="agent-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml urlTransform={defaultUrlTransform}
    components={{
      a: ({ href, children }) => <a href={href} target={href?.startsWith('http') ? '_blank' : undefined} rel="noopener noreferrer">{children}</a>,
      img: ({ src, alt }) => <a href={src} target="_blank" rel="noopener noreferrer">{alt || '图片链接'}</a>,
      table: ({ children }) => <div className="agent-markdown-table"><table>{children}</table></div>,
      pre: ({ children }) => <CodeBlock>{children}</CodeBlock>,
    }}>{content}</ReactMarkdown></div>
}
type Bundle = { bundle_id: string; title: string; files: { name: string; download_url: string }[] }
export const AgentMessage = memo(function AgentMessage({ item, taskId }: { item: MessageSegment; taskId?: string }) {
  const delivery = item.delivery?.payload.delivery as { result_refs?: AgentRef[]; artifact_refs?: AgentRef[]; data?: { delivery?: Bundle[]; limitations?: string[]; draft?: boolean; review_state?: string; full_text?: string; text_download_url?: string; readable_text?: ReadableTextResult[] } } | null
  const unreviewed = delivery?.data?.draft === true || delivery?.data?.review_state === 'unreviewed'
  const readable: ReadableTextResult[] = typeof delivery?.data?.full_text === 'string' ? [{ text: delivery.data.full_text, download_url: delivery.data.text_download_url }] : delivery?.data?.readable_text || []
  const body = <>
    {item.role === 'user' ? <Typography.Paragraph className="agent-user-text">{item.content}</Typography.Paragraph> : readable.length && readable.some(result => result.text === item.content) ? null : <AgentMarkdown content={item.content} />}
    {readable.map((result, index) => <ReadableText key={`${result.result_id || index}:${result.revision || ''}:${result.text_sha256 || ''}`} result={result} taskId={taskId} />)}
    {item.status === 'confirmed' && <ReferenceLinks refs={delivery ? [...(delivery.result_refs || []), ...(delivery.artifact_refs || [])] : item.references} taskId={taskId} />}
    {item.queued && <Tag>将在下一处理边界应用</Tag>}
    {item.truncated && <Typography.Paragraph type="secondary">预览已达到显示上限；完成后显示完整结果。</Typography.Paragraph>}
    {item.status === 'interrupted' && <Alert type="info" title={item.reason || '生成已中断，以上为未确认内容。'} />}
    {delivery?.data?.delivery?.map(bundle => <div className="agent-delivery-bundle" key={bundle.bundle_id}><Typography.Text strong>{bundle.title}</Typography.Text><div className="agent-downloads">{bundle.files.map(file => <Button href={file.download_url} key={file.name}>{file.name}</Button>)}</div></div>)}
    {delivery?.data?.limitations?.map((text, index) => <Typography.Paragraph type="secondary" key={index}>{text}</Typography.Paragraph>)}
  </>
  return <article className={`agent-message agent-stable-message ${item.role === 'user' ? 'agent-message-user' : 'agent-message-assistant'}${item.delivery ? ' agent-message-delivery' : ''}`} data-delivery-status={unreviewed ? 'draft' : item.delivery ? String(item.delivery.payload.status || '') : undefined} data-reading-anchor={item.id} data-message-id={item.id} data-message-status={item.status} data-message-revision={item.revision}>
    <div className="agent-message-label"><Typography.Text type="secondary">{item.role === 'user' ? '你' : 'Agent'}</Typography.Text>
      {item.role === 'assistant' && item.status === 'preview' && <Tag>正在生成 · 未确认</Tag>}
      {item.role === 'assistant' && item.status === 'generated' && <Tag>{item.source === 'text' ? '过程说明' : '已生成 · 等待核实'}</Tag>}
      {item.status === 'interrupted' && <Tag>已中断 · 未确认</Tag>}
    </div>
    {item.delivery && <Typography.Title className="agent-delivery-title" level={3}>{item.source === 'available_result' ? (readable.length ? '原文已完成' : '工具成果已完成') : unreviewed ? '未复核草稿' : statusLabels[String(item.delivery.payload.status)] || '成果'}</Typography.Title>}
    {unreviewed && <Alert type="warning" title="此交付包含未人工核准的算法结果或草稿字段" description="可用于草稿分析；正式结论与核准字段仍需用户确认。" />}
    <div className="agent-message-body">{body}</div>
  </article>
})
