import { Typography } from 'antd'
import { Link } from 'react-router-dom'
import { useWorkspaceId } from '../../App'

export function AuditHelp() {
  const workspace = useWorkspaceId(), scoped = (path: string) => `${path}${workspace ? '?workspace=' + workspace : ''}`
  return <div className="audit-settings"><Typography.Title level={1}>智能审计使用指南</Typography.Title>
    <Typography.Title level={3}>从目标开始</Typography.Title><Typography.Paragraph>在首页描述要解决的问题。可以先讨论审计口径，或者添加本地文件、选择项目资料，再让 Agent 处理。一个目标及其后续补充保存在同一项任务中，刷新后可从“最近任务”继续。</Typography.Paragraph>
    <Typography.Title level={3}>连接大模型</Typography.Title><Typography.Paragraph>首页“连接模型”和设置页使用同一份主控配置。填入支持工具调用的 OpenAI 兼容接口或 Anthropic Messages 接口、模型和 API Key，测试并保存。模型连接前可以保存目标、上传资料和使用手动工具。</Typography.Paragraph>
    <Typography.Title level={3}>工具如何执行</Typography.Title><Typography.Paragraph>Agent 根据目标选择已注册的 ClearData、ClearOCR、ClearClassify、ClearForensics 工具，读取实际返回后决定下一步。普通本地处理、草稿和下载连续执行；有业务歧义时会在对话中询问。工作安排会随处理进展更新。</Typography.Paragraph>
    <Typography.Title level={3}>人工处理后继续</Typography.Title><Typography.Paragraph>在任务区点击“手动接管”，再打开所需工具。Agent 的后续动作会停止，已提交的原生作业可继续完成。保存修改后点击“带回结果”，选择最新结果并填写摘要，回到对话发送继续要求。</Typography.Paragraph>
    <Typography.Title level={3}>证据、发现和底稿</Typography.Title><Typography.Paragraph>直接工具结果可以固定为证据，无需建立处理批次。发现分别记录事实、解释、口径、待补信息和建议；人工可继续编辑。底稿提供 Markdown、可编辑 HTML、发现和证据 JSON，以及来源 CSV。普通草稿不要求先编制或复核，正式人工署名继续使用原有流程。</Typography.Paragraph>
    <Typography.Title level={3}>当前处理范围</Typography.Title><Typography.Paragraph>支持结构化台账精确比对、类型化区间筛选、分组计数及 Decimal 金额汇总。资料检索按原文和已有 OCR 文字匹配，保留文件、页或行来源；未识别图片需先 OCR，当前没有独立的语义向量检索。PDF 表格仅采用工具实际返回的结构，不能把文字猜成表格。取证结果呈现线索与限制，不能自动裁定真伪。</Typography.Paragraph>
    <div className="audit-settings-links"><Link to={scoped('/')}>开始智能审计</Link><Link to={scoped('/settings')}>模型设置</Link><Link to={scoped('/apps')}>手动工具</Link></div>
  </div>
}
