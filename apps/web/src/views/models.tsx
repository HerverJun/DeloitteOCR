import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Card, Empty, Form, Input, InputNumber, Modal, Select, Space, Table, Tag, Typography } from 'antd';
import { DownloadOutlined, ExperimentOutlined, PlusOutlined, CheckCircleOutlined } from '@ant-design/icons';
import { api, post, base, dateText, type Dataset, type Model } from '../api';
import { useCommand, QueryError, type PanelProps } from '../workbench';

const { Title, Text, Paragraph } = Typography;

export function TrainPanel({ project, navigate }: PanelProps) {
  const datasets = useQuery({ queryKey: ['datasets', project.id], queryFn: () => api<Dataset[]>(`/projects/${project.id}/dataset-versions`) });
  const [datasetOpen, setDatasetOpen] = useState(false);
  const [form] = Form.useForm();
  const [versionForm] = Form.useForm();
  const method = Form.useWatch('head_method', form);
  const command = useCommand();

  async function createDataset(values: { name: string; seed: number }) {
    const result = await command.run(() => post<Dataset>(`/projects/${project.id}/dataset-versions`, values), '数据版本已冻结');
    if (result) { setDatasetOpen(false); form.setFieldValue('dataset_id', result.id); versionForm.resetFields(); }
  }
  async function train(values: unknown) {
    const result = await command.run(() => post(`/projects/${project.id}/experiments`, values), '训练任务已提交，可在任务中心查看进度');
    if (result) navigate('jobs');
  }
  return <>
    <div className="page-heading"><div><div className="eyebrow">TRAINING LAB</div><Title level={2}>训练与实验</Title><Paragraph type="secondary">冻结图像编码器，只训练分类头。从一个可复现的基线开始。</Paragraph></div><Button icon={<PlusOutlined />} onClick={() => setDatasetOpen(true)}>发布数据版本</Button></div>
    <QueryError error={datasets.error} />
    <div className="training-columns"><Card title="创建训练实验"><Form form={form} layout="vertical" onFinish={train} initialValues={{ encoder: 'resnet18', head_method: 'adamw', input_size: 224, epochs: 100, batch_size: 16, seed: 42, device: 'cuda', minimum_precision: 0.95 }}>
      <Form.Item name="dataset_id" label="数据版本" rules={[{ required: true, message: '请先发布并选择数据版本' }]}><Select placeholder="选择不可变的数据快照" options={datasets.data?.map(dataset => ({ label: `${dataset.name} · ${dataset.stats.total} 张`, value: dataset.id }))} /></Form.Item>
      <Form.Item name="encoder" label="预训练编码器"><Select options={[{ label: 'ResNet18 · 速度与效果基线', value: 'resnet18' }, { label: 'DINOv2 ViT-S/14 · 通用视觉特征', value: 'dinov2' }, { label: 'MobileNetV4 Conv-S · 小体积', value: 'mobilenetv4' }, { label: 'RepViT M0.9 · 轻量视觉特征', value: 'repvit' }]} /></Form.Item>
      <Form.Item name="head_method" label="分类头方法" extra="正则化搜索只用验证集选择 C；无需设置训练轮数。"><Select options={[{ label: 'AdamW 线性分类头 · 基线', value: 'adamw' }, { label: '逻辑回归 · 验证集正则化搜索', value: 'logistic_cv' }]} /></Form.Item>
      <div className="form-two-columns"><Form.Item name="input_size" label="输入分辨率"><Select options={[{ label: '224 × 224', value: 224 }, { label: '336 × 336', value: 336 }]} /></Form.Item><Form.Item name="device" label="计算设备"><Select options={[{ label: 'NVIDIA GPU', value: 'cuda' }, { label: 'CPU（较慢）', value: 'cpu' }]} /></Form.Item></div>
      <div className="form-two-columns"><Form.Item name="epochs" label="分类头训练轮数"><InputNumber disabled={method === 'logistic_cv'} min={1} max={500} style={{ width: '100%' }} /></Form.Item><Form.Item name="batch_size" label="特征提取批大小"><Select options={[8, 16, 32, 64].map(value => ({ label: `${value} 张`, value }))} /></Form.Item></div>
      <div className="form-two-columns"><Form.Item name="seed" label="随机种子"><InputNumber min={0} max={2147483647} style={{ width: '100%' }} /></Form.Item><Form.Item name="minimum_precision" label="验证集精确率目标"><InputNumber min={0.5} max={1} step={0.01} style={{ width: '100%' }} /></Form.Item></div>
      <Alert type="info" showIcon message="首次训练会下载预训练权重。该精确率目标用于选阈值，不是业务效果承诺。" />
      <Button htmlType="submit" type="primary" size="large" block icon={<ExperimentOutlined />} loading={command.busy} className="start-training">开始训练并导出 ONNX</Button>
    </Form></Card><div className="training-side"><Card title="数据版本"><Table<Dataset> rowKey="id" dataSource={datasets.data} loading={datasets.isPending} pagination={false} size="small" locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="先为每类准备至少 3 个独立来源组" /> }} columns={[{ title: '版本', dataIndex: 'name', render: (value, row) => <div><strong>{value}</strong><div><Text type="secondary">{dateText(row.created_at)}</Text></div></div> }, { title: '划分', render: (_, row) => <Space direction="vertical" size={2}><Text>训练 {row.stats.splits.train}</Text>{row.stats.holdout_policy?.startsWith('external') && <Tag>外部固定划分</Tag>}<Text type="secondary">验证 {row.stats.splits.val} / 测试 {row.stats.splits.test}</Text></Space> }]} /></Card><Card title="实验会保留什么" className="quiet-card"><ul className="check-list"><li><CheckCircleOutlined /> 样本、类别和来源分组快照</li><li><CheckCircleOutlined /> 预训练权重、预处理与环境版本</li><li><CheckCircleOutlined /> 各类指标与混淆矩阵</li><li><CheckCircleOutlined /> ONNX 数值、类别与接受状态校验</li></ul><Paragraph type="secondary">少样本指标会有较大波动。模型选择使用验证集；最终业务报告需要保留独立测试数据。</Paragraph></Card></div></div>
    <Modal title="发布数据版本" open={datasetOpen} onCancel={() => setDatasetOpen(false)} footer={null}><Paragraph type="secondary">将当前已标注图片冻结为快照，按来源分组划分训练、验证与测试集合。后续改标签不会改变此版本。</Paragraph><Form form={versionForm} layout="vertical" initialValues={{ seed: 42 }} onFinish={createDataset}><Form.Item name="name" label="版本名称" rules={[{ required: true }]}><Input placeholder="例如：首批样本 v1" /></Form.Item><Form.Item name="seed" label="划分种子"><InputNumber min={0} max={2147483647} /></Form.Item><div className="modal-actions"><Button onClick={() => setDatasetOpen(false)}>取消</Button><Button type="primary" htmlType="submit" loading={command.busy}>冻结并发布</Button></div></Form></Modal>
  </>;
}

export function ModelsPanel({ project, navigate }: PanelProps) {
  const models = useQuery({ queryKey: ['models', project.id], queryFn: () => api<Model[]>(`/projects/${project.id}/models`), refetchInterval: 5000 });
  const [detail, setDetail] = useState<Model>();
  const command = useCommand();
  async function activate(model: Model) {
    await command.run(() => api(`/projects/${project.id}/active-model`, { method: 'PUT', body: JSON.stringify({ model_id: model.id }) }), '默认模型已更新，仅影响新任务');
  }
  return <>
    <div className="page-heading"><div><div className="eyebrow">MODEL REGISTRY</div><Title level={2}>模型库</Title><Paragraph type="secondary">每一个模型都绑定数据、配置和验证记录，可下载、切换和回退。</Paragraph></div><Button type="primary" icon={<ExperimentOutlined />} onClick={() => navigate('train')}>训练新模型</Button></div>
    <QueryError error={models.error} />
    <Card className="table-card"><Table<Model> rowKey="id" dataSource={models.data} loading={models.isPending} scroll={{ x: 850 }} locale={{ emptyText: <Empty description="还没有模型，先创建一次训练实验" /> }} columns={[
      { title: '模型', dataIndex: 'name', render: (value, row) => <div><strong>{value}</strong>{row.id === project.active_model_id && <Tag color="green" className="inline-tag">默认</Tag>}<div><Text type="secondary">{row.metadata.input.height} px · {dateText(row.created_at)}</Text></div></div> },
      { title: '验证集准确率', render: (_, row) => <div><strong>{(row.metrics.validation.accuracy * 100).toFixed(1)}%</strong><div><Text type="secondary">{row.metrics.validation.sample_count} 张样本</Text></div></div> },
      { title: '验证宏平均 F1', render: (_, row) => row.metrics.validation.macro_f1 === undefined ? '—' : `${(row.metrics.validation.macro_f1 * 100).toFixed(1)}%` },
      { title: 'ONNX 校验', render: (_, row) => <div><Tag icon={<CheckCircleOutlined />} color="green">已通过</Tag><div><Text type="secondary">最大偏差 {row.metrics.export.max_abs_error.toExponential(1)}</Text></div></div> },
      { title: '操作', render: (_, row) => <Space wrap><Button size="small" onClick={() => setDetail(row)}>评估详情</Button><Button size="small" disabled={row.id === project.active_model_id} loading={command.busy} onClick={() => activate(row)}>设为默认</Button><Button size="small" icon={<DownloadOutlined />} href={`${base}/models/${row.id}/download`}>下载</Button></Space> },
    ]} /></Card>
    <div className="small-note">模型列表展示开发验证结果。正式启用自动接受前，请使用业务样本确认误报、漏报与人工复核预算。</div>
    <Modal styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }} title={detail?.name} open={!!detail} onCancel={() => setDetail(undefined)} footer={<Button onClick={() => setDetail(undefined)}>关闭</Button>} width={800}>
      {detail && <><Alert showIcon type="warning" message={detail.metrics.warning} /><div className="metric-summary"><div><Text type="secondary">测试集准确率</Text><strong>{(detail.metrics.test.accuracy * 100).toFixed(1)}%</strong></div><div><Text type="secondary">测试样本</Text><strong>{detail.metrics.test.sample_count}</strong></div><div><Text type="secondary">执行后端</Text><strong className="provider-text">{detail.metrics.export.node_provider_events.CUDAExecutionProvider ? 'CUDA GPU' : 'CPU'}</strong></div></div>{detail.metrics.test.macro_f1 !== undefined && <div className="metric-summary"><div><Text type="secondary">测试宏平均 F1</Text><strong>{(detail.metrics.test.macro_f1 * 100).toFixed(1)}%</strong></div><div><Text type="secondary">自动接受覆盖率</Text><strong>{((detail.metrics.test.coverage ?? 0) * 100).toFixed(1)}%</strong></div><div><Text type="secondary">接受结果精确率</Text><strong>{detail.metrics.test.accepted_precision == null ? '无接受样本' : `${(detail.metrics.test.accepted_precision * 100).toFixed(1)}%`}</strong></div></div>}{detail.metrics.test.accuracy_ci95_wilson && <Paragraph type="secondary">准确率 95% 区间：{detail.metrics.test.accuracy_ci95_wilson.map(value => `${(value * 100).toFixed(1)}%`).join(' – ')}。区间仅描述该测试集，不代表业务保证。</Paragraph>}<Table rowKey="label_id" pagination={{ pageSize: 10, showSizeChanger: false }} size="small" dataSource={detail.metadata.labels} columns={[
        { title: '类别', dataIndex: 'display_name' },
        ...(['precision', 'recall', 'f1-score'] as const).map(key => ({ title: { precision: '精确率', recall: '召回率', 'f1-score': 'F1' }[key], render: (_: unknown, label: Model['metadata']['labels'][number]) => (detail.metrics.test.classification_report[label.label_id]?.[key] * 100).toFixed(1) + '%' })),
        { title: '样本数', render: (_, label) => detail.metrics.test.classification_report[label.label_id]?.support },
        { title: '接受阈值', render: (_, label) => detail.metadata.decision_policy.thresholds_by_label_id[label.label_id].toFixed(3) },
      ]} /><Title level={5}>混淆矩阵</Title><Paragraph type="secondary">行为真实类别，列为预测类别。</Paragraph><ConfusionMatrix model={detail} /></>}
    </Modal>
  </>;
}

function ConfusionMatrix({ model }: { model: Model }) {
  const labels = model.metadata.labels;
  if (labels.length <= 20) return <div className="confusion-wrap"><table className="confusion-table"><thead><tr><th>真实 / 预测</th>{labels.map(label => <th key={label.label_id}>{label.display_name}</th>)}</tr></thead><tbody>{model.metrics.test.confusion_matrix.map((row, i) => <tr key={i}><th>{labels[i].display_name}</th>{row.map((value, j) => <td key={j} className={i === j ? 'diagonal' : ''}>{value}</td>)}</tr>)}</tbody></table></div>;
  const rows: { key: string; truth: string; predicted: string; count: number }[] = [];
  let minimum = 0;
  model.metrics.test.confusion_matrix.forEach((row, i) => row.forEach((count, j) => {
    if (i === j || count <= minimum) return;
    rows.push({ key: `${i}-${j}`, truth: labels[i].display_name, predicted: labels[j].display_name, count });
    rows.sort((a, b) => b.count - a.count);
    if (rows.length > 30) rows.pop();
    if (rows.length === 30) minimum = rows[29].count;
  }));
  return <><Paragraph type="secondary">共 {labels.length} 类，优先展示最多 30 组主要误分。完整矩阵随模型包下载。</Paragraph><Table rowKey="key" size="small" dataSource={rows} pagination={{ pageSize: 10, showSizeChanger: false }} columns={[{ title: '真实类别', dataIndex: 'truth' }, { title: '预测类别', dataIndex: 'predicted' }, { title: '误分张数', dataIndex: 'count' }]} /></>;
}
