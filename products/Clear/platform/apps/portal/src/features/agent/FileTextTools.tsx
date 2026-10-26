import { useRef, type ComponentProps } from 'react'
import { Button, Drawer, Tabs, Typography } from 'antd'
import { DirectImageTextButton } from './DirectImageTextButton'
import { PdfTextForm } from './DirectPdfTextButton'
import './file-text-tools.css'

type Props = Omit<ComponentProps<typeof DirectImageTextButton>, 'action' | 'parameters' | 'buttonType'> & { open: boolean; close: () => void; selectFiles: () => void }

export function FileTextTools({ open, close, selectFiles, ...props }: Props) {
  const chooseAfterClose = useRef(false)
  const accepted = (id: string) => { close(); props.onAccepted(id) }
  return <Drawer title="直接提取文字" rootClassName="agent-file-tools-drawer" size={440} open={open} onClose={close}
    afterOpenChange={visible => { if (!visible && chooseAfterClose.current) { chooseAfterClose.current = false; selectFiles() } }}>
      <div className="agent-file-tools">
        <Typography.Paragraph type="secondary">只需提取原文时，可在这里直接处理。需要整理、比较或分析，回到对话描述需求即可。</Typography.Paragraph>
        <div className="agent-file-selection"><div><span className="agent-file-selection-label">当前资料</span><strong>{props.selectedAssets.length ? `已选择 ${props.selectedAssets.length} 份` : '尚未选择文件'}</strong></div>
          <Button onClick={() => { chooseAfterClose.current = true; close() }}>{props.selectedAssets.length ? '调整附件' : '添加文件'}</Button></div>
        <Tabs items={[
          { key: 'image', label: '图片文字', children: <div className="agent-file-form">
            <Typography.Paragraph type="secondary">选择一张或多张图片，提取可复制的文字。</Typography.Paragraph>
            <div className="agent-file-submit"><DirectImageTextButton {...props} buttonType="primary" onAccepted={accepted} /></div>
          </div> },
          { key: 'pdf', label: 'PDF 指定页', children: <PdfTextForm {...props} onAccepted={accepted} /> },
        ]} />
      </div>
    </Drawer>
}
