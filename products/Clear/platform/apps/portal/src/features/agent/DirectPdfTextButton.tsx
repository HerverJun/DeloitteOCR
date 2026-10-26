import { useId, useState, type ComponentProps } from 'react'
import { Button, Drawer, Input, Select, Typography } from 'antd'
import { DirectImageTextButton, type PdfTextParameters } from './DirectImageTextButton'
import './file-text-tools.css'

type Props = Omit<ComponentProps<typeof DirectImageTextButton>, 'action' | 'parameters'>

export function PdfTextForm(props: Props) {
  const [pages, setPages] = useState(''), [mode, setMode] = useState<PdfTextParameters['mode']>('auto')
  const pagesId = useId(), modeId = useId()
  const entries = pages.trim().split(/[,，、\s]+/), numbers = entries.map(Number)
  const valid = !!pages.trim() && numbers.length <= 100 && entries.every(value => /^\d+$/.test(value)) && numbers.every(value => value >= 1 && value <= 1000) && new Set(numbers).size === numbers.length
  const invalid = !!pages.trim() && !valid
  return <div className="agent-file-form">
    <Typography.Paragraph type="secondary">选择一份 PDF，提取所需页面的文字。</Typography.Paragraph>
    <div className="agent-file-field"><label htmlFor={pagesId}>提取页码</label>
      <Input id={pagesId} aria-label="PDF 页码" aria-describedby={`${pagesId}-hint`} aria-invalid={invalid} status={invalid ? 'error' : undefined} value={pages} placeholder="例如 1, 3, 5" onChange={event => setPages(event.target.value)} />
      <Typography.Text id={`${pagesId}-hint`} type={invalid ? 'danger' : 'secondary'}>{invalid ? '请输入 1–1000 内不重复的整数页码，单次最多 100 页。' : '用逗号分隔页码，单次最多 100 页。'}</Typography.Text>
    </div>
    <Typography.Text type="secondary">{mode === 'auto' ? '自动提取文字，扫描页会自动识别。' : mode === 'native' ? '当前仅提取 PDF 中已有的文字。' : '当前对指定页面进行 OCR 识别。'}</Typography.Text>
    <details className="agent-file-advanced"><summary>高级处理设置{mode !== 'auto' ? ' · 已自定义' : ''}</summary>
    <div className="agent-file-field"><label htmlFor={modeId}>处理方式</label>
    <Select id={modeId} aria-label="PDF 处理方式" value={mode} onChange={setMode} options={[
      { value: 'auto', label: '提取文本，扫描页使用 OCR' }, { value: 'native', label: '仅提取已有文本' }, { value: 'ocr', label: '指定页使用 OCR' },
    ]} />
    </div>
    </details>
    {props.selectedAssets.length !== 1 && <Typography.Text type="secondary">{props.selectedAssets.length ? 'PDF 提取每次处理一份文件，请调整所选资料。' : '先选择一份 PDF，再开始提取。'}</Typography.Text>}
    <div className="agent-file-submit"><DirectImageTextButton {...props} buttonType="primary" action="pdf-text" parameters={{ page_numbers: numbers, mode, engine: 'ppocr' }} disabled={props.disabled || !valid || props.selectedAssets.length !== 1} /></div>
  </div>
}

export function DirectPdfTextButton(props: Props) {
  const [open, setOpen] = useState(false)
  return <><Button onClick={() => setOpen(true)}>PDF 指定页文字提取</Button>
    <Drawer title="PDF 指定页文字提取" rootClassName="agent-file-tools-drawer" size={440} open={open} onClose={() => setOpen(false)}>
      <PdfTextForm {...props} onAccepted={id => { setOpen(false); props.onAccepted(id) }} />
    </Drawer>
  </>
}
