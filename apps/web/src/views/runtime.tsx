import { useQuery } from '@tanstack/react-query';
import { Alert, Card, Descriptions, Progress, Spin, Typography, Tag } from 'antd';
import { DesktopOutlined, DatabaseOutlined } from '@ant-design/icons';
import { api, type Runtime } from '../api';
import { QueryError } from '../workbench';

const { Title, Text, Paragraph } = Typography;

export function RuntimePanel() {
  const query = useQuery({ queryKey: ['runtime'], queryFn: () => api<Runtime>('/runtime'), refetchInterval: 10000 });
  const data = query.data;
  return <>
    <div className="page-heading"><div><div className="eyebrow">LOCAL RUNTIME</div><Title level={2}>运行环境</Title><Paragraph type="secondary">查看当前机器、计算依赖和存储位置。</Paragraph></div></div>
    <QueryError error={query.error} />
    {query.isPending ? <div className="center-loading" role="status" aria-live="polite"><Spin /><Text type="secondary">正在读取运行环境…</Text></div> : !data ? null : <><div className="runtime-grid"><Card title={<><DesktopOutlined /> 计算设备</>}>{data.gpu ? <><Tag color="success" bordered={false}>已检测到 GPU 硬件</Tag><Title level={4}>{data.gpu.name}</Title><div className="runtime-metric"><Text type="secondary">显存使用</Text><strong>{(data.gpu.memory_used_mib / 1024).toFixed(1)} / {(data.gpu.memory_total_mib / 1024).toFixed(1)} GiB</strong></div><Progress percent={Math.round(data.gpu.memory_used_mib / data.gpu.memory_total_mib * 100)} /><Text type="secondary">GPU 利用率 {data.gpu.utilization}%</Text></> : <Alert type="warning" message="没有检测到 NVIDIA 显卡，仍可显式使用 CPU 运行。" />}</Card><Card title={<><DatabaseOutlined /> 数据存储</>}><Title level={3}>{(data.free_bytes / 1024 ** 3).toFixed(1)} <small>GiB 可用</small></Title><Paragraph className="path-text">{data.data_dir}</Paragraph><Text type="secondary">原图、训练数据版本、模型与任务账本按项目保存。</Text></Card></div><Card title="依赖版本" className="top-gap"><Descriptions bordered column={{ xs: 1, sm: 1, md: 2 }} items={[{ key: 'python', label: 'Python', children: data.python }, ...Object.entries(data.packages).map(([name, value]) => ({ key: name, label: name, children: value || '未安装' }))]} /></Card><Alert className="top-gap" type="info" showIcon message="检测到显卡不代表模型一定使用 GPU。每次 ONNX 导出会核验实际执行节点，并把结果写入模型评估记录。" /></>}
  </>;
}
