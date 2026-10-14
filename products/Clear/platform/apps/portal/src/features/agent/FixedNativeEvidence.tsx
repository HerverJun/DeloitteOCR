import { useQuery } from '@tanstack/react-query'
import { Alert, Typography } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { agentRequest } from './api'
import { EvidenceContent } from './EvidenceContent'

export function FixedNativeEvidence() {
  const [params] = useSearchParams(), task = params.get('task_return'), evidence = params.get('evidence')
  const value = useQuery({ queryKey: ['fixed-native-evidence', task, evidence], enabled: !!task && !!evidence,
    queryFn: () => agentRequest<{ summary: string; stale: boolean; observation: unknown; reference: { revision: string | number } }>(`/agent/sessions/${task}/evidence/${evidence}`) })
  if (!task || !evidence) return null
  return <section className="panel" aria-label="固定版本证据"><Typography.Title level={3}>固定版本证据</Typography.Title>
    <Typography.Paragraph type="secondary">这是审计事项保存时的内容。可以继续在下方工具中打开项目并编辑，保存后带回任务。</Typography.Paragraph>
    {value.error && <Alert type="error" title={value.error.message} />}
    {value.data && <>{value.data.stale && <Alert type="warning" title="当前结果已更新，以下保留原证据快照" />}<Typography.Paragraph>{value.data.summary} · 版本 {String(value.data.reference.revision)}</Typography.Paragraph><EvidenceContent observation={value.data.observation} /></>}
  </section>
}
