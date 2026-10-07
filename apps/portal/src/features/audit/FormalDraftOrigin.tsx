import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Modal, Typography } from 'antd'
import { Link } from 'react-router-dom'
import { request } from '../batches/api'
import { auditProjectPath } from './api'
import { EvidenceContent } from '../agent/EvidenceContent'
import type { AuditFindingOrigin, AuditDirectEvidenceRef, DirectEvidenceSnapshot } from '../../../../../packages/contracts/types.generated'

export function FormalDraftOrigin({ origin }: { origin: AuditFindingOrigin }) {
  const author = origin.author
  return <section className="audit-source-draft"><Typography.Title level={4}>转档时的固定草稿</Typography.Title>
    <Typography.Paragraph type="secondary">源草稿 v{origin.source.revision} · 原作者 {String(author.display_name || author.model || author.kind || '未提供')} · 转档操作人 {origin.transfer_actor.display_name}</Typography.Paragraph>
    {([['facts', '观察事实'], ['interpretation', '解释与判断'], ['rule', '采用口径'], ['missing_information', '待补信息'], ['recommendations', '处理建议']] as const).map(([key, label]) => {
      const value = origin.content[key]
      return typeof value === 'string' && value ? <div key={key}><Typography.Text strong>{label}</Typography.Text><Typography.Paragraph style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{value}</Typography.Paragraph></div> : null
    })}
    <details><summary>固定来源身份与原作者</summary><pre className="audit-evidence-json">{JSON.stringify({ source: origin.source, author: origin.author, transfer_actor: origin.transfer_actor, transfer_kind: origin.transfer_kind, transferred_at: origin.transferred_at }, null, 2)}</pre></details>
  </section>
}

export function FormalDirectEvidence({ evidence }: { evidence: AuditDirectEvidenceRef }) {
  const [open, setOpen] = useState(false)
  const snapshot = useQuery({ queryKey: ['formal-direct-evidence', evidence.project_id, evidence.evidence_id, evidence.snapshot_hash], enabled: open,
    queryFn: ({ signal }) => request<DirectEvidenceSnapshot & { stale?: boolean; outdated_reasons?: string[] }>(`${auditProjectPath(evidence.project_id)}/evidence/${encodeURIComponent(evidence.evidence_id)}`, { signal }) })
  return <><Typography.Text>直接工具证据 · 固定来源</Typography.Text>{' '}<Button size="small" onClick={() => setOpen(true)}>读取固定证据</Button>
    <Modal title="正式档案的固定证据" open={open} onCancel={() => setOpen(false)} footer={<Button onClick={() => setOpen(false)}>关闭</Button>} width={820}>
      {snapshot.isPending && <Typography.Paragraph>正在读取固定证据…</Typography.Paragraph>}
      {snapshot.error && <Alert type="error" title={snapshot.error.message} action={<Button onClick={() => void snapshot.refetch()}>重试</Button>} />}
      {snapshot.data && <>{snapshot.data.stale && <Alert type="warning" title="原结果已有更新，以下为转档时的固定证据" />}
        <Typography.Paragraph>{snapshot.data.summary}</Typography.Paragraph><EvidenceContent observation={snapshot.data.observation} />
        <Link to={snapshot.data.location_url}>打开工具中的结果版本</Link>
        <details><summary>完整证据与来源</summary><pre className="audit-evidence-json">{JSON.stringify(snapshot.data, null, 2)}</pre></details>
      </>}
    </Modal>
  </>
}
