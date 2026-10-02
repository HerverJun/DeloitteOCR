import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { App, Alert, Button, Card, Checkbox, Empty, Form, Modal, Pagination, Progress, Select, Space, Table, Tabs, Tag, Typography } from 'antd';
import { DownloadOutlined, PauseOutlined, PlayCircleOutlined, PlusOutlined, ReloadOutlined, StopOutlined, CheckOutlined } from '@ant-design/icons';
import { api, post, base, stateLabels, kindLabels, activeStates, dateText, type Job, type Model, type Result } from '../api';
import { QueryError, useCommand, type PanelProps } from '../workbench';

const { Title, Text, Paragraph } = Typography;
const colors: Record<string, string> = { running: 'processing', succeeded: 'success', failed: 'error', completed_with_errors: 'warning', accepted: 'success', needs_review: 'warning' };
export const Status = ({ value }: { value: string }) => <Tag bordered={false} color={colors[value]}>{stateLabels[value] || value}</Tag>;

export function JobsPanel({ project }: PanelProps) {
  const { modal } = App.useApp();
  const jobs = useQuery({ queryKey: ['jobs', project.id], queryFn: () => api<Job[]>(`/projects/${project.id}/jobs`), refetchInterval: 2000 });
  const models = useQuery({ queryKey: ['models', project.id], queryFn: () => api<Model[]>(`/projects/${project.id}/models`) });
  const [selected, setSelected] = useState<Job>();
  const [createOpen, setCreateOpen] = useState(false);
  const [tab, setTab] = useState('results');
  const [form] = Form.useForm();
  const command = useCommand();
  const current = jobs.data?.find(job => job.id === selected?.id) ?? selected;
  const logs = useQuery({ queryKey: ['logs', current?.id], queryFn: () => api<{ text: string }>(`/jobs/${current!.id}/logs`), enabled: !!current && tab === 'logs', refetchInterval: current && activeStates.includes(current.state) ? 2500 : false });

  async function action(job: Job, action: string) {
    await command.run(() => post(`/jobs/${job.id}/actions`, { action }), '操作已提交');
  }
  async function create(values: unknown) {
    const result = await command.run(() => post<{ job_id: string }>(`/projects/${project.id}/batch-jobs`, values), '分类任务已创建');
    if (result) setCreateOpen(false);
  }

  return <>
    <div className="page-heading"><div><div className="eyebrow">JOBS & REVIEW</div><Title level={2}>分类与复核</Title><Paragraph type="secondary">任务在后台运行，进度持续保存。发现问题后，确认标签并建立下一版数据。</Paragraph></div><Button type="primary" icon={<PlusOutlined />} onClick={() => { form.setFieldsValue({ model_id: project.active_model_id || models.data?.[0]?.id }); setCreateOpen(true); }}>新建分类任务</Button></div>
    <QueryError error={jobs.error || models.error} />
    <Card className="table-card"><Table<Job> rowKey="id" dataSource={jobs.data} loading={jobs.isPending} pagination={{ pageSize: 8, showSizeChanger: false }} scroll={{ x: 920 }} locale={{ emptyText: <Empty description="导入、训练和分类任务会显示在这里" /> }} columns={[
      { title: '任务', render: (_, job) => <div><button className="text-button" onClick={() => { setSelected(job); setTab(job.kind === 'inference' ? 'results' : 'logs'); }}>{kindLabels[job.kind]}</button><div><Text type="secondary" className="mono-small">{job.id.slice(-8)} · {dateText(job.created_at)}</Text></div></div> },
      { title: '状态', render: (_, job) => <Status value={job.state} /> },
      { title: '进度', width: 260, render: (_, job) => <div><div className="job-stage">{job.stage || '等待工作进程'}</div><Progress percent={job.total ? Math.min(100, Math.round((job.done + job.failed) / job.total * 100)) : 0} size="small" status={job.state === 'failed' ? 'exception' : job.state === 'running' ? 'active' : undefined} /><Text type="secondary">{job.done} 完成{job.failed ? ` · ${job.failed} 失败` : ''}{job.total ? ` / ${job.total}` : ''}</Text></div> },
      { title: '操作', render: (_, job) => <Space wrap size={6}><Button size="small" onClick={() => { setSelected(job); setTab(job.kind === 'inference' ? 'results' : 'logs'); }}>详情</Button>
        {job.kind === 'inference' && ['running', 'queued'].includes(job.state) && <Button size="small" icon={<PauseOutlined />} onClick={() => action(job, 'pause')}>暂停</Button>}
        {job.state === 'paused' && <Button size="small" icon={<PlayCircleOutlined />} onClick={() => action(job, 'resume')}>恢复</Button>}
        {['queued', 'running', 'paused', 'pausing'].includes(job.state) && <Button size="small" danger icon={<StopOutlined />} onClick={() => modal.confirm({ title: '取消此任务？', content: '已提交的结果会保留，其余项目停止处理。', okText: '取消任务', cancelText: '继续运行', onOk: () => action(job, 'cancel') })}>取消</Button>}
        {job.kind === 'inference' && ['failed', 'completed_with_errors', 'cancelled'].includes(job.state) && <Button size="small" icon={<ReloadOutlined />} onClick={() => action(job, 'retry_failed')}>重试</Button>}
      </Space> },
    ]} /></Card>
    {current && <Card className="job-detail" title={<Space>{kindLabels[current.kind]}<Status value={current.state} /><Text type="secondary" className="mono-small">{current.id.slice(-8)}</Text></Space>} extra={<Button type="text" onClick={() => setSelected(undefined)}>收起</Button>}>
      {current.error && <Alert showIcon type="error" message={current.error} className="bottom-gap" />}
      {current.kind === 'inference' && current.result && <div className="metric-summary"><div><Text type="secondary">处理吞吐</Text><strong>{Number(current.result.images_per_second_this_run ?? 0).toFixed(1)} 图/秒</strong></div><div><Text type="secondary">总执行时间（含初始化）</Text><strong>{current.result.execution_seconds === undefined ? '—' : `${Number(current.result.execution_seconds).toFixed(2)} 秒`}</strong></div><div><Text type="secondary">模型会话</Text><strong className="provider-text">{current.result.session_cache_hit === undefined ? '未记录' : current.result.session_cache_hit ? '已复用' : '本次加载'}</strong></div></div>}
      <Tabs activeKey={tab} onChange={setTab} items={[
        ...(current.kind === 'inference' ? [{ key: 'results', label: '结果与复核', children: <Results project={project} job={current} /> }] : []),
        { key: 'logs', label: '运行日志', children: <><QueryError error={logs.error} /><pre className="log-view">{logs.isPending ? '正在读取日志…' : logs.error ? '日志加载失败，请切换任务后重试。' : logs.data?.text || '暂无运行日志'}</pre></> },
      ]} />
    </Card>}
    <Modal title="创建批量分类任务" open={createOpen} onCancel={() => setCreateOpen(false)} footer={null} width={560}>
      <Form form={form} layout="vertical" onFinish={create} initialValues={{ batch_size: 32, device: 'cuda', scope: 'unlabeled', auto_accept: false }}>
        <Form.Item name="model_id" label="模型版本" rules={[{ required: true, message: '请先完成一次训练' }]}><Select placeholder="选择模型" options={models.data?.map(model => ({ label: model.name, value: model.id }))} /></Form.Item>
        <Form.Item name="scope" label="图片范围"><Select options={[{ label: '未标注图片', value: 'unlabeled' }, { label: '项目内全部图片', value: 'all' }]} /></Form.Item>
        <div className="form-two-columns"><Form.Item name="batch_size" label="批大小"><Select options={[8, 16, 32, 64].map(value => ({ label: `${value} 张`, value }))} /></Form.Item><Form.Item name="device" label="设备"><Select options={[{ label: 'NVIDIA GPU', value: 'cuda' }, { label: 'CPU', value: 'cpu' }]} /></Form.Item></div>
        <Form.Item name="auto_accept" valuePropName="checked"><Checkbox>按验证集阈值自动接受结果（试验）</Checkbox></Form.Item>
        <Alert type="info" message="默认所有结果进入待复核。开启自动接受后仍需独立抽查；分数不是正确率保证。" showIcon />
        <div className="modal-actions"><Button onClick={() => setCreateOpen(false)}>取消</Button><Button type="primary" htmlType="submit" loading={command.busy}>开始分类</Button></div>
      </Form>
    </Modal>
  </>;
}

function Results({ project, job }: { project: PanelProps['project']; job: Job }) {
  const [filter, setFilter] = useState('all');
  const [page, setPage] = useState(1);
  const [review, setReview] = useState<Result>();
  const [label, setLabel] = useState<string>();
  const command = useCommand();
  const results = useQuery({ queryKey: ['results', job.id, filter, page], queryFn: () => api<{ items: Result[]; total: number }>(`/jobs/${job.id}/results?limit=12&offset=${(page - 1) * 12}${filter === 'all' ? '' : `&status=${filter}`}`), refetchInterval: activeStates.includes(job.state) ? 2500 : false });
  async function confirm() {
    if (!review?.asset_id || !label) return;
    const saved = await command.run(() => post(`/projects/${project.id}/annotations`, { asset_ids: [review.asset_id], label_id: label }), '人工标签已保存');
    if (saved) setReview(undefined);
  }
  return <>
    <div className="data-toolbar"><Space><Select aria-label="筛选分类结果" value={filter} style={{ width: 150 }} options={[{ label: '全部结果', value: 'all' }, ...['needs_review', 'accepted', 'failed', 'pending'].map(value => ({ label: stateLabels[value], value }))]} onChange={value => { setFilter(value); setPage(1); }} /><Text type="secondary">{results.data?.total ?? 0} 项</Text></Space><Space><Button icon={<DownloadOutlined />} href={`${base}/jobs/${job.id}/export?format=csv`}>CSV</Button><Button href={`${base}/jobs/${job.id}/export?format=jsonl`}>JSONL</Button></Space></div>
    <QueryError error={results.error} />
    {results.data?.items.length ? <><div className="result-grid">{results.data.items.map(row => <article className="result-card" key={row.id}><div className="result-image">{row.asset_id && <img loading="lazy" src={`${base}/assets/${row.asset_id}/thumbnail`} alt={row.name || '分类图片'} />}</div><div className="result-info"><Text ellipsis title={row.name}>{row.name || row.path.split(/[\\/]/).pop()}</Text><div><Status value={row.prediction?.status || row.state} />{row.prediction && <Text>{row.prediction.display_name} · {(row.prediction.score * 100).toFixed(1)} 分</Text>}</div>{row.error && <Text type="danger" ellipsis title={row.error}>{row.error}</Text>}{row.confirmed_label && <Text type="secondary">已标注：{project.labels.find(item => item.label_id === row.confirmed_label)?.display_name}</Text>}<Button size="small" icon={<CheckOutlined />} disabled={!row.asset_id || row.state !== 'succeeded'} onClick={() => { setReview(row); setLabel(row.confirmed_label || row.prediction?.label_id); }}>查看并确认</Button></div></article>)}</div><div className="pagination"><Pagination current={page} pageSize={12} total={results.data.total} showSizeChanger={false} onChange={setPage} /></div></> : <Empty description={results.isPending ? '正在读取结果' : '暂无符合条件的结果'} />}
    <Modal title="人工确认标签" open={!!review} onCancel={() => setReview(undefined)} width={680} footer={<Space><Button onClick={() => setReview(undefined)}>跳过</Button><Button type="primary" disabled={!label} loading={command.busy} onClick={confirm}>确认并保存</Button></Space>}>
      {review && <><div className="review-image"><img src={`${base}/assets/${review.asset_id}/image`} alt={review.name} /></div><Paragraph>{review.name} <a href={`${base}/assets/${review.asset_id}/image`} target="_blank" rel="noreferrer">打开原图</a></Paragraph><Paragraph type="secondary">预测：{review.prediction?.display_name}，分数 {((review.prediction?.score || 0) * 100).toFixed(1)}。请根据图片内容确认真实类别。</Paragraph><Select aria-label="人工确认类别" value={label} onChange={setLabel} options={project.labels.map(item => ({ label: item.display_name, value: item.label_id }))} style={{ width: '100%' }} /><Paragraph type="secondary" className="review-note">保存后建立标注修订。原始预测和旧实验保持不变；重新发布数据版本后可用于下一轮训练。</Paragraph></>}
    </Modal>
  </>;
}
