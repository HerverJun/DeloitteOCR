import { Typography } from 'antd'
import type { AppHealth, AppManifest, Health } from '../../../../../packages/contracts/types.generated'

const capabilities: Record<string, string> = {
  import: '导入', export: '导出', cancel: '取消', pause: '暂停', resume: '继续',
  retry_failed: '重试失败项', gpu: 'GPU 运算',
}
const resources: Record<string, string> = { cpu: 'CPU', gpu: 'GPU', network: '网络' }
const formats: Record<string, string> = {
  'image/png': 'PNG 图像', 'image/jpeg': 'JPEG 图像', 'image/bmp': 'BMP 图像',
  'image/tiff': 'TIFF 图像', 'image/webp': 'WebP 图像', 'image/heic': 'HEIC 图像',
  'image/heif': 'HEIF 图像', 'application/pdf': 'PDF', 'application/json': 'JSON',
  'application/zip': 'ZIP', 'text/plain': 'TXT', 'text/markdown': 'Markdown',
  'application/x-ndjson': 'NDJSON', 'text/html': 'HTML', 'application/vnd.apache.parquet': 'Parquet',
  'text/csv': 'CSV', 'text/tab-separated-values': 'TSV',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'XLSX',
  'application/vnd.ms-excel': 'XLS',
}
const reasons: Record<string, string> = {
  'Service has not been probed': '服务尚未检测', 'Service identity verified': '服务身份已核实',
  'UI has not been probed': '界面尚未检测', 'CPU has not been probed': 'CPU 执行状态尚未检测',
  'GPU has not been probed': 'GPU 运行状态尚未检测', 'GPU is not declared': '工具未声明 GPU 使用能力',
  'GPU is not available': '工具尚未安装，GPU 能力未核实',
  'GPU service is not installed': '工具尚未安装，GPU 能力未核实',
  'UI bundle is not installed': '应用界面尚未安装',
  'CPU service is not installed': '应用运行服务尚未安装',
  'Model files are not installed': '模型文件尚未安装',
  'Model files have not been probed': '模型文件尚未检测', 'Models have not been probed': '模型尚未检测',
  'Application bundle is not registered': '本机尚未登记工具包',
  'Service exited': '服务已退出', 'Service monitoring stopped': '服务监控已停止',
  'Service exited or was stopped': '服务已退出或停止',
  'Supervisor stopped monitoring': '服务监控已停止，当前状态未确认',
}

export function healthStateText(state: Health['state'] | undefined) {
  return ({ ready: '已就绪', unavailable: '不可用', disabled: '未启用', unknown: '未确认' })[state || 'unknown']
}
export function healthReasonText(reason: string | undefined) {
  return reason ? reasons[reason] || reason : ''
}

type Declaration = Pick<AppManifest, 'capabilities' | 'resources' | 'input_types' | 'output_types'>
export const dataDeclaration: Declaration = {
  capabilities: ['import', 'export', 'cancel'], resources: ['cpu'],
  input_types: ['text/csv', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.apache.parquet'],
  output_types: ['text/csv', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/vnd.apache.parquet', 'application/json'],
}
export const forensicsDeclaration: Declaration = {
  capabilities: ['import', 'export', 'resume'], resources: ['cpu'],
  input_types: ['image/jpeg', 'image/png', 'image/webp', 'image/bmp', 'image/tiff'],
  output_types: ['application/json', 'text/html', 'image/png', 'application/zip'],
}
export function ApplicationDeclaration({ declaration, health }: { declaration: Declaration; health?: AppHealth | null }) {
  const names = (items: string[], mapping: Record<string, string>, empty: string) =>
    items.map(item => mapping[item] || item).join('、') || empty
  return <details className="application-details"><summary>查看工具声明</summary>
    <Typography.Paragraph type="secondary">
      输入格式：{names(declaration.input_types, formats, '由工具报告')}<br />
      输出格式：{names(declaration.output_types, formats, '由工具报告')}<br />
      支持操作：{names(declaration.capabilities, capabilities, '由工具报告')}<br />
      支持资源：{names(declaration.resources, resources, '由工具报告')}
    </Typography.Paragraph>
    {declaration.resources.includes('gpu') && <Typography.Paragraph type="secondary">
      GPU 状态：{healthStateText(health?.gpu.state)}{health?.gpu.reason ? ` · ${healthReasonText(health.gpu.reason)}` : ''}<br />
      设备可用性、占用和模型加载以工具检测及任务执行状态为准。
    </Typography.Paragraph>}
    {declaration.resources.includes('network') && <Typography.Paragraph type="secondary">
      网络用于已配置的外部模型连接。
    </Typography.Paragraph>}
    <Typography.Paragraph type="secondary">任务当前可执行的操作以任务状态为准。</Typography.Paragraph>
  </details>
}
