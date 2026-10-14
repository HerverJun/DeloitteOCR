import { Table, Typography } from 'antd'

type Cell = { kind: string; value: string | boolean | null; raw?: string | null }
type Data = { rows?: { source_row?: number; cells?: Cell[]; category?: string; [key: string]: unknown }[]; metadata?: { columns?: { column_id: string; header: string }[] }; snapshot?: { edited?: { text?: string }; original?: { text?: string } }; items?: { prediction_label_id?: string; human_label_id?: string | null; asset_id?: string }[] }
export function EvidenceContent({ observation }: { observation: unknown }) {
  const value = observation as Data
  if (value.rows?.length && value.rows[0].cells) return <Table size="small" scroll={{ x: true }} pagination={false} dataSource={value.rows} rowKey={(_, i) => String(i)} columns={[
    { title: '来源行', dataIndex: 'source_row' }, ...(value.metadata?.columns || []).map((column, index) => ({ title: column.header || column.column_id, key: column.column_id,
      render: (_: unknown, row: NonNullable<Data['rows']>[number]) => { const cell = row.cells?.[index]; return <span title={cell?.raw || ''}>{cell?.value === null ? cell.kind === 'unknown_formula' ? '公式结果未知' : cell.kind === 'error' ? '计算未完成' : '空值' : String(cell?.value ?? '')}</span> } })),
  ]} />
  if (value.rows?.length) return <Table size="small" scroll={{ x: true }} pagination={false} dataSource={value.rows} rowKey={(_, i) => String(i)} columns={Object.keys(value.rows[0]).filter(key => key === 'category' || key.endsWith('_source_row') || key.endsWith('_value') || key === 'different_columns_json').map(key => ({ title: ({ category: '核对结果', left_source_row: '左表来源行', right_source_row: '右表来源行', different_columns_json: '差异字段' } as Record<string, string>)[key] || key.replace('_value', ''), dataIndex: key }))} />
  if (value.snapshot) return <><Typography.Title level={5}>识别文字</Typography.Title><Typography.Paragraph style={{ whiteSpace: 'pre-wrap' }}>{value.snapshot.edited?.text || value.snapshot.original?.text || '本结果没有可读取的正文。'}</Typography.Paragraph></>
  if (value.items) return <Table size="small" pagination={false} rowKey={(_, i) => String(i)} dataSource={value.items} columns={[{ title: '资料', dataIndex: 'asset_id' }, { title: '模型类别', dataIndex: 'prediction_label_id' }, { title: '人工类别', dataIndex: 'human_label_id' }]} />
  return <Typography.Paragraph>这项证据保留了原资料或完整报告，可从来源链接查看。</Typography.Paragraph>
}
