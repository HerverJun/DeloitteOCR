import { randomUUID } from '../requestId'
import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { App, Alert, Button, Card, Checkbox, Empty, Form, Input, Modal, Pagination, Progress, Select, Space, Table, Tabs, Tag, Typography, Spin } from 'antd';
import { DownloadOutlined, PauseOutlined, PlayCircleOutlined, PlusOutlined, ReloadOutlined, StopOutlined, CheckOutlined, ClockCircleOutlined, CheckCircleOutlined, ExclamationCircleOutlined } from '@ant-design/icons';
import { api, post, base, stateLabels, kindLabels, activeStates, dateText, type Job, type Model, type Result } from '../api';
import { QueryError, useCommand, type PanelProps } from '../workbench';
import { DeviceReadiness, useDeviceReadiness } from './DeviceReadiness';

const { Title, Text, Paragraph } = Typography;
const colors: Record<string, string> = { queued: 'default', running: 'processing', pausing: 'warning', paused: 'warning',
  succeeded: 'success', failed: 'error', completed_with_errors: 'warning', accepted: 'success', needs_review: 'warning',
  confirmed: 'success', deferred: 'warning', reopened: 'default' };
export const Status = ({ value }: { value: string }) => {
  const icon = ['queued', 'pending'].includes(value) ? <ClockCircleOutlined /> : ['paused', 'pausing'].includes(value) ? <PauseOutlined /> :
    ['succeeded', 'accepted', 'confirmed'].includes(value) ? <CheckCircleOutlined /> : ['failed', 'completed_with_errors'].includes(value) ? <ExclamationCircleOutlined /> : undefined;
  return <Tag bordered={false} color={colors[value]} icon={icon}>{stateLabels[value] || value}</Tag>;
};

function JobProgress({ job }: { job: Job }) {
  const knownTotal = job.total > 0;
  return <div><div className="job-stage">{job.stage || '等待工作进程'}</div>{knownTotal ?
    <Progress percent={Math.min(100, Math.round((job.done + job.failed) / job.total * 100))} size="small" status={job.state === 'failed' ? 'exception' : job.state === 'running' ? 'active' : undefined} /> :
    <div className="job-indeterminate" role="status">{job.state === 'running' && <Spin size="small" />}<Text type="secondary">{activeStates.includes(job.state) ? '处理总量待返回' : '处理总量未记录'}</Text></div>}
    <Text type="secondary">{job.done} 完成{job.failed ? ` · ${job.failed} 失败` : ''}{knownTotal ? ` / ${job.total}` : ''}</Text></div>;
}


export function JobsPanel({ project, focusJobId }: PanelProps & { focusJobId?: string }) {
  const { modal } = App.useApp();
  const jobs = useQuery({ queryKey: ['jobs', project.id], queryFn: () => api<Job[]>(`/projects/${project.id}/jobs`), refetchInterval: 2000 });
  const models = useQuery({ queryKey: ['models', project.id], queryFn: () => api<Model[]>(`/projects/${project.id}/models`) });
  const [selected, setSelected] = useState<string | undefined>(focusJobId);
  const [historyKind, setHistoryKind] = useState(focusJobId ? 'all' : 'inference');
  const [createOpen, setCreateOpen] = useState(false);
  const [tab, setTab] = useState(focusJobId ? 'logs' : 'results');
  const [form] = Form.useForm();
  const device = Form.useWatch('device', form) || 'cuda';
  const runtime = useDeviceReadiness(createOpen, device);
  const command = useCommand();
  const current = jobs.data?.find(job => job.id === selected);
  const logs = useQuery({ queryKey: ['logs', current?.id], queryFn: () => api<{ text: string }>(`/jobs/${current!.id}/logs`), enabled: !!current && tab === 'logs', refetchInterval: current && activeStates.includes(current.state) ? 2500 : false });

  async function action(job: Job, action: string) {
    await command.run(() => post(`/jobs/${job.id}/actions`, { action }), '操作已提交');
  }
  async function create(values: unknown) {
    const result = await command.run(async () => { await runtime.verifyDevice('inference'); return post<{ job_id: string }>(`/projects/${project.id}/batch-jobs`, values); }, '分类任务已创建');
    if (result) {
      setSelected(result.job_id);
      setTab('results');
      setCreateOpen(false);
    }
  }

  return <>
    <div className="page-heading"><div><div className="eyebrow">JOBS & REVIEW</div><Title level={2}>分类与复核</Title><Paragraph type="secondary">任务在后台运行，进度持续保存。发现问题后，确认标签并建立下一版数据。</Paragraph></div><Button type="primary" icon={<PlusOutlined />} onClick={() => { form.setFieldsValue({ model_id: project.active_model_id || models.data?.[0]?.id }); setCreateOpen(true); }}>新建分类任务</Button></div>
    <QueryError error={jobs.error || models.error} />
    <Card className="table-card" extra={<Select aria-label="执行记录类型" value={historyKind} onChange={setHistoryKind} options={[{value: 'inference', label: '分类任务'}, {value: 'all', label: '全部执行记录'}]} />}><Table<Job> rowKey="id" dataSource={jobs.data?.filter(job => historyKind === 'all' || job.kind === 'inference')} loading={jobs.isPending} pagination={{ pageSize: 8, showSizeChanger: false }} scroll={{ x: 920 }} locale={{ emptyText: <Empty description="导入、训练和分类任务会显示在这里" /> }} columns={[
      { title: '任务', render: (_, job) => <div><button className="text-button" onClick={() => { setSelected(job.id); setTab(job.kind === 'inference' ? 'results' : 'logs'); }}>{kindLabels[job.kind]}</button><div><Text type="secondary" className="mono-small">{job.id.slice(-8)} · {dateText(job.created_at)}</Text></div></div> },
      { title: '状态', render: (_, job) => <Status value={job.state} /> },
      { title: '进度', width: 260, render: (_, job) => <JobProgress job={job} /> },
      { title: '操作', render: (_, job) => <Space wrap size={6}><Button size="small" onClick={() => { setSelected(job.id); setTab(job.kind === 'inference' ? 'results' : 'logs'); }}>详情</Button>
        {job.kind === 'inference' && ['running', 'queued'].includes(job.state) && <Button size="small" icon={<PauseOutlined />} onClick={() => action(job, 'pause')}>暂停</Button>}
        {job.state === 'paused' && <Button size="small" icon={<PlayCircleOutlined />} onClick={() => action(job, 'resume')}>恢复</Button>}
        {['queued', 'running', 'paused', 'pausing'].includes(job.state) && <Button size="small" danger icon={<StopOutlined />} onClick={() => modal.confirm({ title: '取消此任务？', content: '已提交的结果会保留，其余项目停止处理。', okText: '取消任务', cancelText: '继续运行', onOk: () => action(job, 'cancel') })}>取消</Button>}
        {job.kind === 'inference' && ['failed', 'completed_with_errors', 'cancelled'].includes(job.state) && <Button size="small" icon={<ReloadOutlined />} onClick={() => action(job, 'retry_failed')}>重试</Button>}
      </Space> },
    ]} /></Card>
    {current && <Card className="job-detail" title={<Space>{kindLabels[current.kind]}<Status value={current.state} /><Text type="secondary" className="mono-small">{current.id.slice(-8)}</Text></Space>} extra={<Button type="text" onClick={() => setSelected(undefined)}>收起</Button>}>
      <JobProgress job={current} />
      {current.error && <Alert showIcon type="error" message={current.error} className="bottom-gap" />}
      {current.kind === 'inference' && current.result && <div className="metric-summary"><div><Text type="secondary">处理吞吐</Text><strong>{Number(current.result.images_per_second_this_run ?? 0).toFixed(1)} 图/秒</strong></div><div><Text type="secondary">总执行时间（含初始化）</Text><strong>{current.result.execution_seconds === undefined ? '—' : `${Number(current.result.execution_seconds).toFixed(2)} 秒`}</strong></div><div><Text type="secondary">模型会话</Text><strong className="provider-text">{current.result.session_cache_hit === undefined ? '未记录' : current.result.session_cache_hit ? '已复用' : '本次加载'}</strong></div></div>}
      <Tabs activeKey={tab} onChange={setTab} items={[
        ...(current.kind === 'inference' ? [{ key: 'results', label: '结果与复核', children: <Results key={current.id} project={project} job={current} /> }] : []),
        { key: 'logs', label: '运行日志', children: <><QueryError error={logs.error} /><pre className="log-view">{logs.isPending ? '正在读取日志…' : logs.error ? '日志加载失败，请切换任务后重试。' : logs.data?.text || '暂无运行日志'}</pre></> },
      ]} />
    </Card>}
    <Modal title="创建批量分类任务" open={createOpen} onCancel={() => setCreateOpen(false)} footer={null} width={560}>
      <Form form={form} layout="vertical" onFinish={create} initialValues={{ batch_size: 32, device: 'cuda', scope: 'unlabeled', auto_accept: false }}>
        <Form.Item name="model_id" label="模型版本" rules={[{ required: true, message: '请选择可用模型' }]}><Select placeholder="选择模型" options={models.data?.map(model => ({ label: model.name, value: model.id }))} /></Form.Item>
        <Form.Item name="scope" label="图片范围"><Select options={[{ label: '未标注图片', value: 'unlabeled' }, { label: '项目内全部图片', value: 'all' }]} /></Form.Item>
        <div className="form-two-columns"><Form.Item name="batch_size" label="批大小"><Select options={[8, 16, 32, 64].map(value => ({ label: `${value} 张`, value }))} /></Form.Item><Form.Item name="device" label="设备"><Select options={[{ label: 'NVIDIA GPU', value: 'cuda' }, { label: 'CPU', value: 'cpu' }]} /></Form.Item></div>
        <DeviceReadiness runtime={runtime.data} kind="inference" device={device} loading={runtime.isPending} error={runtime.error} refresh={() => { void runtime.refreshDevices(); }} />
        <Form.Item name="auto_accept" valuePropName="checked"><Checkbox>按验证集阈值自动接受结果（试验）</Checkbox></Form.Item>
        <Alert type="info" message="默认所有结果进入待复核。开启自动接受后仍需独立抽查；分数不是正确率保证。" showIcon />
        <div className="modal-actions"><Button onClick={() => setCreateOpen(false)}>取消</Button><Button type="primary" htmlType="submit" disabled={device === 'cuda' && (runtime.isPending || !!runtime.error) || runtime.data?.devices?.inference[device as 'cpu' | 'cuda'].available === false} loading={command.busy}>开始分类</Button></div>
      </Form>
    </Modal>
  </>;
}

function Results({ project, job }: { project: PanelProps['project']; job: Job }) {
  type PendingDisposition = { item_ids: string[]; state: string; label_id: string | null;
    note: string; expected_revisions: Record<string, number>; request_id: string; operation_id: string;
    instance_id: string; actor: { id: string; display_name: string; source: 'local' }; intent: 'manual' };
  const storageKey = `classification-dispositions:${project.id}:${job.id}`;
  const pending = useRef<Record<string, PendingDisposition> | undefined>(undefined);
  if (!pending.current) {
    try { pending.current = JSON.parse(sessionStorage.getItem(storageKey) || '{}'); }
    catch { pending.current = {}; }
  }
  const [filter, setFilter] = useState('needs_review');
  const [page, setPage] = useState(1);
  const [review, setReview] = useState<Result>();
  const reviewEpoch = useRef(0);
  const closeReview = () => { reviewEpoch.current += 1; setReview(undefined); };
  const [label, setLabel] = useState<string>();
  const [note, setNote] = useState('');
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [bulkLabel, setBulkLabel] = useState<string>();
  const command = useCommand();
  const results = useQuery({ queryKey: ['results', job.id, filter, page], queryFn: () => api<{ items: Result[]; total: number; disposition_counts: Record<string, number> }>(`/jobs/${job.id}/results?limit=12&offset=${(page - 1) * 12}${filter === 'all' ? '' : `&status=${filter}`}`), refetchInterval: activeStates.includes(job.state) ? 2500 : false });
  function open(row: Result) { reviewEpoch.current += 1; setReview(row); setLabel(row.human_label_id || row.prediction?.label_id); setNote(row.disposition_note || ''); }
  async function dispose(state: 'confirmed' | 'deferred' | 'reopened', rows: Result[], next = false) {
    if (!rows.length || (state === 'confirmed' && !(next ? label : bulkLabel))) return;
    const epoch = reviewEpoch.current;
    const actor = { id: 'local-classification-user', display_name: '本机分类操作者', source: 'local' as const };
    const choice = { item_ids: rows.map(row => row.id).sort(), state,
      label_id: state === 'confirmed' ? (next ? label : bulkLabel)! : null, note: next ? note : '', actor };
    const signature = JSON.stringify(choice);
    const saved = await command.run(async () => {
      const requests = pending.current!;
      if (!requests[signature]) {
        const identity = await api<{ instance_id: string }>('/health');
        requests[signature] = { ...choice, expected_revisions: Object.fromEntries(rows.map(row => [row.id, row.disposition_revision])),
          request_id: `request_${randomUUID().replaceAll('-', '')}`,
          operation_id: `operation_${randomUUID().replaceAll('-', '')}`,
          instance_id: identity.instance_id, intent: 'manual' };
        sessionStorage.setItem(storageKey, JSON.stringify(requests));
      }
      // Preserve the original revisions and identity when a response is lost.
      const response = await fetch(`${base}/jobs/${job.id}/dispositions`, { method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Platform-Request': '1' },
        body: JSON.stringify(requests[signature]) });
      const receipt = await response.json();
      if (!response.ok) {
        if (response.status === 409) {
          delete requests[signature];
          sessionStorage.setItem(storageKey, JSON.stringify(requests));
        }
        throw new Error(typeof receipt.detail === 'string' ? receipt.detail : '处置未保存，请刷新结果并重新核对');
      }
      delete requests[signature];
      sessionStorage.setItem(storageKey, JSON.stringify(requests));
      return receipt;
    }, `${rows.length} 项处置已保存`);
    if (!saved) return;
    setSelectedIds([]);
    const fresh = (await results.refetch()).data;
    if (next && epoch === reviewEpoch.current) {
      const remaining = fresh?.items.find(row => !rows.some(savedRow => savedRow.id === row.id) && row.state === 'succeeded' && row.disposition_state !== 'confirmed');
      if (remaining) open(remaining);
      else { setReview(undefined); if (page > 1 && !fresh?.items.length) setPage(page - 1); }
    }
  }
  const selectable = results.data?.items.filter(row => row.state === 'succeeded') ?? [];
  const selectedRows = results.data?.items.filter(row => selectedIds.includes(row.id)) ?? [];
  return <>
    <div className="result-identity"><Text type="secondary">处置身份：本机分类操作者（本机来源，不代表组织认证）。响应丢失时重复同一操作会核对原请求。</Text>
    <Button type="link" onClick={() => void results.refetch()}>刷新结果并重新核对</Button></div>
    <div className="data-toolbar"><Space wrap><Select aria-label="筛选分类结果" value={filter} style={{ width: 150 }} options={[{ label: '全部结果', value: 'all' }, ...['needs_review', 'confirmed', 'deferred', 'reopened', 'failed', 'pending', 'accepted'].map(value => ({ label: stateLabels[value], value }))]} onChange={value => { setFilter(value); setPage(1); setSelectedIds([]); }} /><Text type="secondary">{results.data?.total ?? 0} 项 · 本次确认 {results.data?.disposition_counts.confirmed ?? 0} · 暂缓 {results.data?.disposition_counts.deferred ?? 0}</Text></Space><Space><Button icon={<DownloadOutlined />} href={`${base}/jobs/${job.id}/export?format=csv`}>CSV</Button><Button href={`${base}/jobs/${job.id}/export?format=jsonl`}>JSONL</Button></Space></div>
    <Space wrap className="selection-toolbar result-selection" role="group" aria-label="批量处置操作"><Checkbox checked={!!selectable.length && selectedIds.length === selectable.length} indeterminate={!!selectedIds.length && selectedIds.length < selectable.length} onChange={event => setSelectedIds(event.target.checked ? selectable.map(row => row.id) : [])}>选择当前页可复核结果</Checkbox><Text type="secondary">已选当前页 {selectedRows.length} 项</Text><Select aria-label="批量确认类别" value={bulkLabel} onChange={setBulkLabel} placeholder="确认类别" style={{ width: 160 }} options={project.labels.map(item => ({ label: item.display_name, value: item.label_id }))} /><Button disabled={!selectedRows.length || !bulkLabel || command.busy} onClick={() => void dispose('confirmed', selectedRows)}>确认所选 {selectedRows.length} 项</Button><Button disabled={!selectedRows.length || command.busy} onClick={() => void dispose('deferred', selectedRows)}>暂缓所选</Button><Button disabled={!selectedRows.length || command.busy} onClick={() => void dispose('reopened', selectedRows)}>重开所选</Button></Space>
    <QueryError error={results.error} />
    {results.data?.items.length ? <><div className="result-grid">{results.data.items.map(row => <article className={`result-card ${selectedIds.includes(row.id) ? 'selected' : ''}`} key={row.id}><div className="result-image">{row.asset_id && <img loading="lazy" src={`${base}/assets/${row.asset_id}/thumbnail`} alt={row.name || '分类图片'} />}</div><div className="result-info"><Checkbox disabled={row.state !== 'succeeded'} checked={selectedIds.includes(row.id)} onChange={event => setSelectedIds(event.target.checked ? [...selectedIds, row.id] : selectedIds.filter(id => id !== row.id))}><Text ellipsis title={row.name}>{row.name || row.path.split(/[\\/]/).pop()}</Text></Checkbox><div className="result-status"><Status value={row.disposition_state} /></div>{row.prediction && <div className="result-prediction"><strong>原始建议：{row.prediction.display_name}</strong><span className="result-score">模型分数 {(row.prediction.score * 100).toFixed(1)} · 不代表审计风险</span></div>}{row.error && <Text type="danger" ellipsis title={row.error}>{row.error}</Text>}{row.human_label_id && <Text>本次确认：{project.labels.find(item => item.label_id === row.human_label_id)?.display_name}</Text>}{row.confirmed_label && <Text type="secondary">资产当前标签：{project.labels.find(item => item.label_id === row.confirmed_label)?.display_name}</Text>}<Button size="small" icon={<CheckOutlined />} disabled={!row.asset_id || row.state !== 'succeeded'} onClick={() => open(row)}>查看并处置</Button></div></article>)}</div><div className="pagination"><Pagination current={page} pageSize={12} total={results.data.total} showSizeChanger={false} onChange={value => { setPage(value); setSelectedIds([]); }} /></div></> : <Empty description={results.isPending ? '正在读取结果' : '暂无符合条件的结果'} />}
    <Modal title="核对本次分类结果" open={!!review} onCancel={closeReview} width={960} footer={<Space wrap><Button onClick={closeReview}>关闭</Button><Button aria-label="暂缓并下一项" loading={command.busy} onClick={() => review && void dispose('deferred', [review], true)}>暂缓并下一项</Button><Button aria-label="重开并下一项" loading={command.busy} onClick={() => review && void dispose('reopened', [review], true)}>重开并下一项</Button><Button aria-label="保存并下一项" type="primary" disabled={!label} loading={command.busy} onClick={() => review && void dispose('confirmed', [review], true)}>保存并下一项</Button></Space>}>
      {review && <div className="review-layout"><div className="review-image"><img src={`${base}/assets/${review.asset_id}/image`} alt={review.name} /></div><div className="review-context"><Paragraph>{review.name} <a href={`${base}/assets/${review.asset_id}/image`} target="_blank" rel="noreferrer">打开原图</a></Paragraph><Paragraph>原始建议：{review.prediction?.display_name}，分数 {((review.prediction?.score || 0) * 100).toFixed(1)}。</Paragraph><Paragraph type="secondary">分数不代表审计风险；请根据原图确认真实类别。</Paragraph><Select aria-label="人工确认类别" value={label} onChange={setLabel} options={project.labels.map(item => ({ label: item.display_name, value: item.label_id }))} style={{ width: '100%' }} /><Input.TextArea aria-label="人工处置说明" value={note} onChange={event => setNote(event.target.value)} maxLength={2000} placeholder="处置说明（可选）" rows={3} /><Paragraph type="secondary" className="review-note">本次处置单独保存。暂缓和重开仍计入待处理；原始预测持续保留。</Paragraph></div></div>}
    </Modal>
  </>;
}
