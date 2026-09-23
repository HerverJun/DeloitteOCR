import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Alert, Button, Checkbox, Empty, Form, Input, Modal, Pagination, Select, Space, Spin, Tag, Typography } from 'antd';
import { FolderOpenOutlined, PlusOutlined, UploadOutlined, ExperimentOutlined, CheckOutlined } from '@ant-design/icons';
import { api, post, base, type Asset } from '../api';
import { useCommand, QueryError, type PanelProps } from '../workbench';

const { Title, Text, Paragraph } = Typography;

export function AssetsPanel({ project, navigate }: PanelProps) {
  const [filter, setFilter] = useState('all');
  const [search, setSearch] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<string[]>([]);
  const [importOpen, setImportOpen] = useState(false);
  const [assign, setAssign] = useState<string>();
  const [group, setGroup] = useState('');
  const [form] = Form.useForm();
  const uploadRef = useRef<HTMLInputElement>(null);
  const command = useCommand();
  const query = useQuery({ queryKey: ['assets', project.id, page, filter, search], queryFn: () => api<{ items: Asset[]; total: number }>(`/projects/${project.id}/assets?limit=24&offset=${(page - 1) * 24}&search=${encodeURIComponent(search)}${filter === 'all' ? '' : `&label=${filter}`}`), refetchInterval: 5000 });
  const labels = project.labels.map(label => ({ label: label.display_name, value: label.label_id }));

  async function importDirectory(values: { path: string; infer_labels: boolean; source_id: string }) {
    const result = await command.run(() => post(`/projects/${project.id}/imports`, values), '导入任务已创建');
    if (result) { setImportOpen(false); navigate('jobs'); }
  }

  async function upload(files: FileList | null) {
    if (!files?.length) return;
    const data = new FormData();
    [...files].forEach(file => data.append('files', file));
    if (!['all', 'unlabeled'].includes(filter)) data.append('label_id', filter);
    const result = await command.run(() => api<{ asset_ids: string[]; errors: { name: string; error: string }[] }>(`/projects/${project.id}/uploads`, { method: 'POST', body: data }));
    if (result?.errors.length) Modal.warning({ title: `已导入 ${result.asset_ids.length} 张，${result.errors.length} 张失败`, content: result.errors.slice(0, 5).map(error => <p key={error.name}>{error.name}: {error.error}</p>) });
    if (uploadRef.current) uploadRef.current.value = '';
  }

  async function annotate() {
    if (assign === undefined) return;
    const result = await command.run(() => post(`/projects/${project.id}/annotations`, { asset_ids: selected, label_id: assign === 'unlabeled' ? null : assign, group_id: group.trim() || null }), '标注已保存，旧数据版本不受影响');
    if (result) { setSelected([]); setGroup(''); }
  }

  return <>
    <div className="page-heading"><div><div className="eyebrow">DATA WORKSPACE</div><Title level={2}>图像与标注</Title><Paragraph type="secondary">整理类别、检查样本，为下一次训练准备可靠的数据。</Paragraph></div><Space><Button icon={<UploadOutlined />} loading={command.busy} onClick={() => uploadRef.current?.click()}>上传图片</Button><Button type="primary" icon={<FolderOpenOutlined />} onClick={() => setImportOpen(true)}>导入文件夹</Button></Space></div>
    <input ref={uploadRef} type="file" multiple accept=".jpg,.jpeg,.png,.webp,.bmp" className="visually-hidden" aria-label="上传图片文件" onChange={event => void upload(event.target.files)} />
    <div className="data-toolbar"><Space wrap><Select aria-label="筛选类别" value={filter} options={[{ label: '全部类别', value: 'all' }, { label: '未标注', value: 'unlabeled' }, ...labels]} style={{ width: 160 }} onChange={value => { setFilter(value); setPage(1); setSelected([]); }} /><Input.Search allowClear placeholder="搜索图片名称" aria-label="搜索图片" style={{ width: 250 }} onSearch={value => { setSearch(value); setPage(1); }} /><Text type="secondary">{query.data?.total ?? 0} 张图片</Text></Space><Button icon={<ExperimentOutlined />} onClick={() => navigate('train')}>准备训练</Button></div>
    <QueryError error={query.error} />
    {selected.length > 0 && <div className="selection-toolbar"><Text>已选 {selected.length} 张</Text><Select aria-label="设置选中图片类别" placeholder="设置类别" options={[...labels, { label: '移至未标注', value: 'unlabeled' }]} value={assign} onChange={setAssign} style={{ width: 160 }} /><Input placeholder="来源分组（可选）" aria-label="来源分组" value={group} onChange={event => setGroup(event.target.value)} style={{ width: 180 }} /><Button type="primary" icon={<CheckOutlined />} disabled={assign === undefined} loading={command.busy} onClick={annotate}>保存标注</Button><Button type="text" onClick={() => setSelected([])}>取消选择</Button></div>}
    {query.isPending ? <div className="center-loading"><Spin /></div> : query.data?.items.length ? <><div className="asset-grid">{query.data.items.map(asset => <article key={asset.id} className={`asset-card ${selected.includes(asset.id) ? 'selected' : ''}`}>
      <div className="asset-image"><img loading="lazy" src={`${base}/assets/${asset.id}/thumbnail`} alt={asset.name} onError={event => { event.currentTarget.style.opacity = '0.15'; }} /><Checkbox className="asset-check" aria-label={`选择 ${asset.name}`} checked={selected.includes(asset.id)} onChange={event => setSelected(previous => event.target.checked ? [...previous, asset.id] : previous.filter(id => id !== asset.id))} /></div>
      <div className="asset-caption"><Text ellipsis title={asset.name}>{asset.name}</Text><div><Tag bordered={false} color={asset.label_id ? 'green' : 'default'}>{project.labels.find(label => label.label_id === asset.label_id)?.display_name || '未标注'}</Tag><Text type="secondary">{asset.width} × {asset.height}</Text></div></div>
    </article>)}</div><div className="pagination"><Pagination current={page} pageSize={24} total={query.data.total} showSizeChanger={false} onChange={value => { setPage(value); setSelected([]); }} /></div></> : <div className="empty-panel"><Empty description={search || filter !== 'all' ? '没有符合条件的图片' : '还没有图片，导入你的第一组样本'} /><Button type="primary" icon={<PlusOutlined />} onClick={() => setImportOpen(true)}>导入图片</Button></div>}
    <div className="small-note">相同文档的不同截图、连拍或近重复图请设置相同来源分组。系统会保持整组不跨训练、验证与测试集合。</div>
    <Modal open={importOpen} title="从本机文件夹导入" onCancel={() => setImportOpen(false)} footer={null} width={590}>
      <Alert type="info" showIcon message="使用服务器上的文件夹路径，原图保持只读。支持 JPEG、PNG、静态 WebP 和 BMP。" />
      <Form form={form} layout="vertical" className="spaced-form" initialValues={{ infer_labels: true }} onFinish={importDirectory}>
        <Form.Item name="path" label="文件夹完整路径" rules={[{ required: true, message: '请输入文件夹路径' }]}><Input placeholder="E:/datasets/my-images" /></Form.Item>
        <Form.Item name="source_id" label="来源名称" extra="例如客户批次、采集设备或截图来源。"><Input placeholder="可选，默认使用文件夹名称" /></Form.Item>
        <Form.Item name="infer_labels" valuePropName="checked"><Checkbox>根据第一级子文件夹名称匹配类别</Checkbox></Form.Item>
        <Paragraph type="secondary">可匹配：{project.labels.slice(0, 12).map(label => `${label.label_id} 或 ${label.display_name}`).join('；')}{project.labels.length > 12 ? `；共 ${project.labels.length} 类，其余可在类别筛选中查看` : ''}。未匹配的图片进入未标注池。</Paragraph>
        <div className="modal-actions"><Button onClick={() => setImportOpen(false)}>取消</Button><Button type="primary" htmlType="submit" loading={command.busy}>开始导入</Button></div>
      </Form>
    </Modal>
  </>;
}
