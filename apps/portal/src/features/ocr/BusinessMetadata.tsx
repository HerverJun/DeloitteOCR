import { Alert, Button, Input, Space, Typography } from 'antd'

export type MetadataItem = {
  kind: string; text: string; source?: string; table?: number | null; box?: number[];
  alternative_text?: string; alternative_source?: string; human_confirmed?: boolean; review_required?: boolean;
  review_reason?: string; primary_source?: Record<string, unknown>;
  transcription_evidence?: { primary_text?: string; auxiliary_text?: string; primary_source?: Record<string, unknown>; auxiliary_source?: Record<string, unknown> };
}
export type MetadataDraft = { after: string; choice: 'manual' | 'primary' | 'auxiliary' }
export type MetadataEdit = MetadataDraft & { item_index: number; before: string }
export function metadataEdits(items: MetadataItem[], drafts: Record<number, MetadataDraft>): MetadataEdit[] {
  return Object.entries(drafts).map(([index, draft]) => ({ item_index: Number(index), before: items[Number(index)].text, ...draft }))
}

export function BusinessMetadata({ items, drafts, disabled, change }: {
  items: MetadataItem[]; drafts: Record<number, MetadataDraft>; disabled: boolean; change: (index: number, draft: MetadataDraft) => void
}) {
  return <section aria-label="业务说明复核"><Typography.Title level={4}>标题、单位与表注</Typography.Title>
    <Typography.Paragraph type="secondary">当前文字、另一识别原文和来源均保留。选择候选或手改后，保存到本图当前修订；确认仅针对本次加载的版本。</Typography.Paragraph>
    {!items.length && <Typography.Paragraph type="secondary">没有已定位的业务说明；请结合原图核对是否遗漏。</Typography.Paragraph>}
    {items.map((part, index) => {
      const evidence = part.transcription_evidence, primary = evidence?.primary_text, auxiliary = evidence?.auxiliary_text
      const primarySource = evidence?.primary_source || part.primary_source
      return <div key={index} style={{ borderTop: '1px solid #e5e7eb', padding: '12px 0' }}>
        <Typography.Text strong>{({ title: '标题', unit: '单位', note: '表注', caption: '表题' } as Record<string, string>)[part.kind] || part.kind} {index + 1}{part.table != null ? ` · 表 ${part.table + 1}` : ''}</Typography.Text>
        <Typography.Paragraph>当前文字：{part.text}</Typography.Paragraph>
        <Typography.Paragraph type="secondary">当前来源：{part.source || '来源未确定'} · {part.human_confirmed ? '人工已确认' : part.review_required ? '待人工核对' : '识别候选，未逐项人工确认'}{part.box ? ` · 原图坐标 ${part.box.join(', ')}` : ''}</Typography.Paragraph>
        {part.alternative_text !== undefined && <Typography.Paragraph>另一识别原文：{part.alternative_text}（{part.alternative_source || '来源未确定'}）</Typography.Paragraph>}
        {part.review_required && <Alert type="warning" title="业务说明仍需核对" description={part.review_reason || '来源或识别文字存在疑点'} />}
        {evidence && <><Typography.Paragraph>主识别原文：{primary || '无可选原文'}</Typography.Paragraph>
          <Typography.Paragraph>辅助识别原文：{auxiliary || '无可选原文'}</Typography.Paragraph>
          <details><summary>查看识别来源</summary><Typography.Paragraph code>主识别来源：{JSON.stringify(primarySource || {})}</Typography.Paragraph><Typography.Paragraph code>辅助识别来源：{JSON.stringify(evidence.auxiliary_source || {})}</Typography.Paragraph></details></>}
        <Space wrap style={{ margin: '8px 0' }}>
          <Button disabled={disabled || primary === undefined || !primarySource} onClick={() => change(index, { after: primary!, choice: 'primary' })}>选用主识别原文 {index + 1}</Button>
          <Button disabled={disabled || auxiliary === undefined} onClick={() => change(index, { after: auxiliary!, choice: 'auxiliary' })}>选用辅助识别原文 {index + 1}</Button>
          {drafts[index] && <Typography.Text type="secondary">{({ manual: '手工修改', primary: '选用主识别', auxiliary: '选用辅助识别' })[drafts[index].choice]}，等待保存</Typography.Text>}
        </Space>
        <Input.TextArea aria-label={`业务说明${index + 1}复核值`} value={drafts[index]?.after ?? part.text} disabled={disabled} autoSize={{ minRows: 2, maxRows: 8 }} maxLength={10000} onChange={event => change(index, { after: event.target.value, choice: 'manual' })} />
      </div>
    })}
  </section>
}
