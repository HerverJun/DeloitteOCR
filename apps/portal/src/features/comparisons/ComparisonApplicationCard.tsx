import { Button, Card, Typography } from 'antd'
import { Link, useSearchParams } from 'react-router-dom'
import { ToolIdentity } from '../catalog/ToolIdentity'

export function ComparisonApplicationCard() {
  const [params] = useSearchParams(), path = `/apps/comparisons${params.size ? `?${params}` : ''}`
  return <Card className="application-card" title={<ToolIdentity appId="comparisons" to={path} label="文档主题对比" />}>
    <Typography.Paragraph>比较扫描 PDF 与幻灯片中的指定信息，保留证据并输出统一报告。</Typography.Paragraph>
    <Typography.Paragraph type="secondary">本地 OCR / 逐页多模态 · 自定义字段 · HTML 报告</Typography.Paragraph>
    <Link to={path}><Button type="primary">打开主题对比</Button></Link>
  </Card>
}
