import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Space, Typography } from 'antd'
import { useSearchParams } from 'react-router-dom'
import { agentRequest } from './api'
import { FixedNativeEvidence } from './FixedNativeEvidence'
import { ManualReturn } from './ManualReturn'
import { useNativeWindow } from '../catalog/nativeWindow'

type Preview = { application: 'ocr' | 'classification'; link_id: string; next_offset: number | null;
  data: { text_lines?: string[]; items?: Record<string, unknown>[]; table_count?: number; total: number } }

export function NativeResultPreview() {
  const [params] = useSearchParams(), workspace = params.get('workspace'), binding = params.get('native_binding'), task = params.get('task_return')
  const [offsets, setOffsets] = useState([0]), offset = offsets[offsets.length - 1]
  const native = useNativeWindow(`${workspace}:${binding}`)
  const value = useQuery({ queryKey: ['native-result-preview', workspace, binding, task, offset], enabled: !!workspace && !!binding,
    queryFn: () => agentRequest<Preview>(`/workspaces/${workspace}/native-results/${binding}?offset=${offset}&limit=20${task ? `&session_id=${task}` : ''}`) })
  function openNative() {
    if (!value.data) return
    native.open(async signal => {
      const answer = await agentRequest<{ url: string }>(`/workspaces/${workspace}/${value.data!.application}/links/${value.data!.link_id}/navigation`, { method: 'POST', signal })
      return { url: answer.url, kind: 'ticket' }
    })
  }
  return <div className="ocr-workflow"><FixedNativeEvidence /><Typography.Title level={2}>原生工具结果</Typography.Title>
    <Typography.Paragraph type="secondary">原件已核实并接回平台。以下显示当前保存内容；固定证据保留其原版本。</Typography.Paragraph>
    {(value.error || native.error) && <Alert type="error" title={value.error?.message || native.error} />}
    {value.data && <section className="panel"><Space><Button loading={native.opening} onClick={openNative}>打开原生项目继续操作</Button>
      <Button onClick={() => void value.refetch()}>读取最新保存结果</Button></Space>
      {value.data.data.text_lines?.map((line, index) => <Typography.Paragraph key={offset + index} style={{ whiteSpace: 'pre-wrap' }}>{line}</Typography.Paragraph>)}
      {value.data.data.items?.map((item, index) => <Typography.Paragraph key={offset + index}>{String(item.source_name || item.asset_id)} · 预测 {String(item.prediction_label_id || '无')} · 标签 {String(item.human_label_id || item.current_annotation_label_id || '未标注')} · {String(item.state)}</Typography.Paragraph>)}
      {value.data.data.table_count !== undefined && <Typography.Paragraph>结构表 {value.data.data.table_count} 张，可在原生项目继续编辑或由 Agent 读取及处理。</Typography.Paragraph>}
      <Space><Button disabled={offsets.length === 1} onClick={() => setOffsets(old => old.slice(0, -1))}>上一页</Button>
        <Button disabled={value.data.next_offset == null} onClick={() => setOffsets(old => [...old, value.data!.next_offset!])}>下一页</Button></Space>
    </section>}
    <ManualReturn taskId={task || undefined} workspaceId={workspace || undefined} />
  </div>
}
