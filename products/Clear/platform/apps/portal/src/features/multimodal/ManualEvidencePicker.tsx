import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Select, Space, Typography } from 'antd'
import { request } from '../batches/api'

export type ManualSelection = { manual_bundle_id: string }
type Bundle = { kind: string; bundle_id: string; title: string; review_revision?: number }

export function ManualEvidencePicker({ workspaceId, disabled, selected, onSelected }: {
  workspaceId: string; disabled: boolean; selected: ManualSelection[]; onSelected: (values: ManualSelection[]) => void;
}) {
  const [refresh, setRefresh] = useState(0)
  const catalog = useQuery({ queryKey: ['manual-evidence-catalog', workspaceId, refresh], queryFn: async ({ signal }) => {
    const result: Bundle[] = []
    let offset: number | null = 0
    while (offset !== null) {
      const page: { items: Bundle[]; next_offset: number | null } = await request(`/workspaces/${encodeURIComponent(workspaceId)}/artifacts?offset=${offset}&limit=100`, { signal })
      result.push(...page.items.filter(item => item.kind === 'manual_analysis'))
      if (page.next_offset !== null && page.next_offset <= offset) throw new Error('成果分页无效')
      offset = page.next_offset
    }
    return result
  } })
  return <div className="audit-manual-evidence"><Space><Typography.Text strong>多模态人工复核成果</Typography.Text><Button disabled={disabled} onClick={() => setRefresh(value => value + 1)}>刷新成果</Button></Space>
    <Select style={{ width: '100%', marginBlock: 12 }} mode="multiple" aria-label="采用多模态人工复核成果" loading={catalog.isPending} disabled={disabled}
      placeholder="选择已保存并归集的印章、人脸、音频或深度取证成果" value={selected.map(value => value.manual_bundle_id)}
      onChange={values => onSelected(values.map(manual_bundle_id => ({ manual_bundle_id })))}
      options={catalog.data?.map(item => ({ value: item.bundle_id, label: `${item.title} · 复核 v${item.review_revision}` }))} />
    {catalog.error && <Alert type="error" title={catalog.error.message} />}
  </div>
}
