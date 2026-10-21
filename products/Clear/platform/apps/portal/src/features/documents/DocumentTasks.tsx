import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Alert, Button, Empty, Space, Table, Tag, Typography } from 'antd'
import { Link } from 'react-router-dom'
import { stateNames } from '../batches/api'
import { documentsApi, type DocumentRun } from './api'

export function DocumentTasks({ workspace }: { workspace: string }) {
  const [page, setPage] = useState(1)
  const tasks = useQuery({ queryKey: ['document-tasks', workspace, page],
    queryFn: () => documentsApi.list(workspace, (page - 1) * 10, 10), refetchInterval: 15000 })
  return <section className="panel" aria-label="文档解析任务">
    <div className="section-head"><div><Typography.Title level={3}>文档解析任务</Typography.Title>
      <Typography.Text type="secondary">查看原件对应的解析进度、保存结果与表格校对。</Typography.Text></div>
      <Button loading={tasks.isFetching} onClick={() => void tasks.refetch()}>刷新文档任务</Button></div>
    {tasks.error ? <Alert type="error" title="文档任务暂时无法读取" description={tasks.error.message} /> :
      <Table<DocumentRun> rowKey="document_id" size="small" loading={tasks.isPending} dataSource={tasks.data?.items || []}
        pagination={{ current: page, pageSize: 10, total: tasks.data?.total || 0, showSizeChanger: false, onChange: setPage }}
        locale={{ emptyText: <Empty description="当前项目尚无文档解析任务" /> }} columns={[
          { title: '原件', render: (_, run) => run.source?.original_name || run.original_name || run.document_id },
          { title: '状态', render: (_, run) => <Tag>{stateNames[run.status] || run.status}</Tag> },
          { title: '运行编号', render: (_, run) => <Typography.Text code>{run.document_id}</Typography.Text> },
          { title: '操作', render: (_, run) => <Space><Link to={`/apps/ocr/documents?${new URLSearchParams({ workspace, document: run.document_id })}`}>查看解析结果</Link></Space> },
        ]} />}
  </section>
}
