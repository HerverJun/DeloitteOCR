import { useState, type ComponentProps } from 'react'
import { Input, Select, Space, Typography } from 'antd'
import { DirectImageTextButton, type PdfTextParameters } from './DirectImageTextButton'

export function DirectPdfTextButton(props: Omit<ComponentProps<typeof DirectImageTextButton>, 'action' | 'parameters'>) {
  const [pages, setPages] = useState(''), [mode, setMode] = useState<PdfTextParameters['mode']>('auto')
  const entries = pages.trim().split(/[,，、\s]+/), numbers = entries.map(Number)
  const valid = pages.trim() && numbers.length <= 100 && entries.every(value => /^\d+$/.test(value)) && numbers.every(value => value >= 1 && value <= 1000) && new Set(numbers).size === numbers.length
  return <details className="agent-pdf-action"><summary>PDF 指定页文字提取</summary><Space orientation="vertical" size="small">
    <Typography.Text type="secondary">选择一份 PDF，填写页码，例如 2,5。每次最多 100 页。</Typography.Text>
    <Input aria-label="PDF 页码" value={pages} placeholder="例如 2,5" onChange={event => setPages(event.target.value)} />
    <Select aria-label="PDF 处理方式" value={mode} onChange={setMode} style={{ minWidth: 220 }} options={[
      { value: 'auto', label: '提取文本，扫描页使用 OCR' }, { value: 'native', label: '仅提取已有文本' }, { value: 'ocr', label: '指定页使用 OCR' },
    ]} />
    <DirectImageTextButton {...props} action="pdf-text" parameters={{ page_numbers: numbers, mode, engine: 'ppocr' }} disabled={props.disabled || !valid || props.selectedAssets.length !== 1} />
  </Space></details>
}
