import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  App, Alert, Button, Card, Divider, Empty, Form, Input, Layout, Menu, Modal, Select, Space,
  Spin, Tag, Typography, Dropdown,
} from 'antd';
import {
  AppstoreOutlined, ArrowRightOutlined, CheckCircleOutlined, DatabaseOutlined,
  FolderOpenOutlined, PictureOutlined, PlusOutlined, ExperimentOutlined, DeploymentUnitOutlined,
  SettingOutlined, ThunderboltOutlined, GithubOutlined,
  ExportOutlined,
} from '@ant-design/icons';
import { api, post, type Project, type Runtime } from './api';
import { brands, clearTheme } from './clear-ui/theme';
import { AssetsPanel, TrainPanel, ModelsPanel, JobsPanel, RuntimePanel } from './panels';
import { navigationFromLaunch, safeReturnUrl, savedPlatformNavigation, savePlatformNavigation,
  type PlatformNavigation } from './platformNavigation';

const { Title, Text, Paragraph } = Typography;
export type Page = 'overview' | 'assets' | 'train' | 'models' | 'jobs' | 'runtime';
export type PanelProps = { project: Project; navigate: (page: Page) => void };

export function useCommand() {
  const { message } = App.useApp();
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  async function run<T>(fn: () => Promise<T>, success?: string): Promise<T | undefined> {
    setBusy(true);
    try {
      const value = await fn();
      await client.invalidateQueries();
      if (success) message.success(success);
      return value;
    } catch (error) {
      message.error(error instanceof Error ? error.message : '操作失败');
      return undefined;
    } finally { setBusy(false); }
  }
  return { run, busy };
}

export function QueryError({ error }: { error: unknown }) {
  return error ? <Alert showIcon type="error" message={error instanceof Error ? error.message : '加载失败'} /> : null;
}

const menu = [
  { key: 'overview', icon: <AppstoreOutlined />, label: '项目概览' },
  { key: 'assets', icon: <PictureOutlined />, label: '图像与标注' },
  { key: 'train', icon: <ExperimentOutlined />, label: '训练与实验' },
  { key: 'models', icon: <DeploymentUnitOutlined />, label: '模型库' },
  { key: 'jobs', icon: <ThunderboltOutlined />, label: '分类与复核' },
];

export function Workbench() {
  const [page, setPage] = useState<Page>(() => sessionStorage.getItem('prism-platform-page') === 'assets' && savedPlatformNavigation() ? 'assets' : 'overview');
  const [selectedId, setSelectedId] = useState(() => localStorage.getItem('prism-project') || '');
  const [navigation, setNavigation] = useState<PlatformNavigation | null>(null);
  const [navigationApplied, setNavigationApplied] = useState(false);
  const [navigationError, setNavigationError] = useState('');
  const [launchReady, setLaunchReady] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [form] = Form.useForm();
  const command = useCommand();
  const projects = useQuery({ queryKey: ['projects'], queryFn: () => api<Project[]>('/projects'), refetchInterval: 5000 });
  const nativeProject = navigation && projects.data?.some(p => p.id === navigation.module_project_id)
    ? navigation.module_project_id : null;
  const projectId = (!navigationApplied && nativeProject) || (projects.data?.some(p => p.id === selectedId) ? selectedId : projects.data?.[0]?.id);
  const detail = useQuery({ queryKey: ['project', projectId], queryFn: () => api<Project>(`/projects/${projectId}`), enabled: !!projectId, refetchInterval: 4000 });
  const runtime = useQuery({ queryKey: ['runtime'], queryFn: () => api<Runtime>('/runtime'), refetchInterval: 20000 });
  const project = detail.data;

  useEffect(() => {
    let active = true;
    const launch = navigationFromLaunch();
    if (!launch) { setNavigation(savedPlatformNavigation()); setLaunchReady(true); return; }
    launch.then(value => {
      if (!active) return;
      savePlatformNavigation(value);
      setNavigation(value);
      setPage(value.asset_id ? 'assets' : 'overview');
      setLaunchReady(true);
    }).catch(() => { if (active) { setNavigationError('平台入口已失效，请从平台重新打开分类应用。'); setLaunchReady(true); } });
    return () => { active = false; };
  }, []);
  useEffect(() => {
    if (!navigation || !projects.data || navigationApplied) return;
    if (projects.data.some(p => p.id === navigation.module_project_id)) setSelectedId(navigation.module_project_id);
    else setNavigationError('平台关联的分类项目不存在；未切换原生项目。');
    setNavigationApplied(true);
  }, [navigation, projects.data, navigationApplied]);
  useEffect(() => {
    if (!navigation) return;
    try { sessionStorage.setItem('prism-platform-page', page); } catch { /* no storage */ }
  }, [navigation, page]);
  useEffect(() => { if (projectId) localStorage.setItem('prism-project', projectId); }, [projectId]);

  async function create(values: { name: string; description: string; labels: string }) {
    const lines = values.labels.split('\n').map(line => line.trim()).filter(Boolean);
    const labels = lines.map(line => {
      const [label_id, ...rest] = line.split(/[,，]/);
      return { label_id: label_id.trim(), display_name: rest.join('，').trim() || label_id.trim(), definition: '' };
    });
    const result = await command.run(() => post<Project>('/projects', { ...values, labels }), '项目已创建');
    if (result) { setSelectedId(result.id); setCreateOpen(false); setPage('assets'); form.resetFields(); }
  }

  const title = page === 'runtime' ? '运行环境' : menu.find(item => item.key === page)?.label;
  return <Layout className="workbench">
    <Layout.Sider width={224} className="sidebar" theme="dark" breakpoint="lg" collapsedWidth={72}>
      <div className="brand" title={`${brands.classification} · 图像分类与复核`}><img className="brand-mark" src="/brand/clear-mark.svg" alt="" /><div className="brand-text"><strong>{brands.classification}</strong><span>图像分类与复核</span></div></div>
      <div className="project-switch">
        <Text className="sidebar-label">当前项目</Text>
        <Select aria-label="切换项目" value={projectId} placeholder="选择项目" options={projects.data?.map(p => ({ label: p.name, value: p.id }))} onChange={id => { setSelectedId(id); setPage('overview'); }} popupMatchSelectWidth={260} />
        <Button icon={<PlusOutlined />} block onClick={() => setCreateOpen(true)}>新建项目</Button>
      </div>
      <div className="sidebar-label nav-label">工作空间</div>
      <Menu theme="dark" selectedKeys={[page]} items={menu} onClick={({ key }) => setPage(key as Page)} />
      <div className="sidebar-bottom">
        <Menu theme="dark" selectedKeys={[page]} items={[{ key: 'runtime', icon: <SettingOutlined />, label: '运行环境' }]} onClick={() => setPage('runtime')} />
        <div className="local-status"><span className="status-dot" /><span>本地工作空间</span><Tag bordered={false}>0.2</Tag></div>
      </div>
    </Layout.Sider>
    <Layout>
      <header className="topbar"><div className="topbar-context"><Text type="secondary">工作空间</Text><span className="breadcrumb-slash">/</span><Text>{title}</Text>{project && <Text className="topbar-project" ellipsis title={project.name}>{project.name}</Text>}</div><Space size={12} className="topbar-actions"><Dropdown menu={{ items: [...(projects.data?.map(p => ({ key: p.id, label: p.name })) ?? []), { key: 'new-project', label: '新建项目', icon: <PlusOutlined /> }], onClick: ({ key }) => { if (key === 'new-project') setCreateOpen(true); else { setSelectedId(key); setPage('overview'); } } }} trigger={['click']}><Button size="small" className="mobile-project-control" icon={<FolderOpenOutlined />}>项目</Button></Dropdown><span className="gpu-top"><span className={`status-dot ${runtime.data?.gpu ? '' : 'muted'}`} />{runtime.data?.gpu ? 'GPU 已连接' : 'CPU 模式'}</span>{navigation && safeReturnUrl(navigation) && <a href={safeReturnUrl(navigation)!} target="_blank" rel="noopener noreferrer" className="api-link platform-return"><ExportOutlined /> 返回平台</a>}<a href="/docs" target="_blank" rel="noreferrer" className="api-link api-docs"><GithubOutlined /> API 文档</a><span className="local-indicator">本地</span></Space></header>
      <main className="main-content">
        {navigationError && <Alert type="warning" showIcon message={navigationError} closable onClose={() => setNavigationError('')} />}
        <QueryError error={projects.error || detail.error} />
        {!launchReady || projects.isPending || (projectId && detail.isPending) ? <div className="center-loading" role="status" aria-live="polite"><Spin /><Text type="secondary">正在读取项目…</Text></div> : page === 'runtime' ? <RuntimePanel /> : !project ?
          <section className="welcome"><span className="welcome-icon"><PictureOutlined /></span><Text type="secondary">从你的第一组图片开始</Text><Title level={1}>让图像各归其类</Title><Paragraph>创建一个分类项目，导入少量样本，训练属于你业务的模型。<br />图片、模型和结果均保留在本机。</Paragraph><Button type="primary" size="large" icon={<PlusOutlined />} onClick={() => setCreateOpen(true)}>创建第一个项目</Button><div className="welcome-steps"><span>01 定义类别</span><span>02 导入样本</span><span>03 训练与分类</span></div></section> :
          <>
            {page === 'overview' && <Overview project={project} navigate={setPage} />}
            {page === 'assets' && <AssetsPanel key={`assets-${project.id}`} project={project} navigate={setPage} focusAssetId={navigation?.module_project_id === project.id ? navigation.asset_id : null} />}
            {page === 'train' && <TrainPanel key={`train-${project.id}`} project={project} navigate={setPage} />}
            {page === 'models' && <ModelsPanel key={`models-${project.id}`} project={project} navigate={setPage} />}
            {page === 'jobs' && <JobsPanel key={`jobs-${project.id}`} project={project} navigate={setPage} />}
          </>}
      </main>
      <footer className="footer"><span>{brands.classification} · 图像分类与复核</span><span>数据留在本机，结果可追溯</span></footer>
    </Layout>
    <Modal title="创建分类项目" open={createOpen} onCancel={() => setCreateOpen(false)} footer={null} width={580} destroyOnHidden>
      <Paragraph type="secondary">每个项目拥有独立的类别、数据集与模型。首版采用一张图片一个类别。</Paragraph>
      <Form form={form} layout="vertical" onFinish={create} initialValues={{ labels: 'class_a,类别 A\nclass_b,类别 B' }}>
        <Form.Item name="name" label="项目名称" rules={[{ required: true, whitespace: true, message: '请输入项目名称' }]}><Input placeholder="例如：文档截图分类" maxLength={100} /></Form.Item>
        <Form.Item name="description" label="项目说明"><Input.TextArea rows={2} placeholder="说明分类目标与容易混淆的情况" /></Form.Item>
        <div className="form-label-row"><Text>类别定义</Text><Button type="link" size="small" onClick={() => form.setFieldValue('labels', 'invoice,发票\npayment_qr,收款码截图\ncontract,合同\nother,其他')}>填入文档四分类</Button></div>
        <Form.Item name="labels" extra="每行一个类别：英文 ID,显示名称。ID 使用小写字母、数字或下划线，创建后保持固定。" rules={[{ required: true }]}><Input.TextArea rows={5} className="mono-input" aria-label="类别定义" /></Form.Item>
        <div className="modal-actions"><Button onClick={() => setCreateOpen(false)}>取消</Button><Button htmlType="submit" type="primary" loading={command.busy}>创建项目</Button></div>
      </Form>
    </Modal>
  </Layout>;
}

function Overview({ project, navigate }: PanelProps) {
  const counts = project.asset_counts ?? [];
  const total = counts.reduce((sum, item) => sum + item.count, 0);
  const labeled = counts.filter(item => item.label_id).reduce((sum, item) => sum + item.count, 0);
  const running = project.job_counts?.filter(item => ['running', 'queued'].includes(item.state)).reduce((sum, item) => sum + item.count, 0) ?? 0;
  return <>
    <div className="page-heading"><div><div className="eyebrow">PROJECT OVERVIEW</div><Title level={2}>{project.name}</Title><Paragraph type="secondary">{project.description || '从数据准备到批量分类，在一个工作空间内完成。'}</Paragraph></div><Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('assets')}>导入图片</Button></div>
    <section className="summary-grid">
      {[['图像总数', total.toLocaleString(), <PictureOutlined />, `${labeled.toLocaleString()} 张已标注`], ['分类类别', project.labels.length, <AppstoreOutlined />, '独立定义的互斥类别'], ['可用模型', project.model_count ?? 0, <DeploymentUnitOutlined />, project.active_model_id ? '已设置默认模型' : '等待第一次训练'], ['活动任务', running, <ThunderboltOutlined />, running ? '任务正在后台处理' : '当前没有排队任务']].map(([name, value, icon, note]) => <div className="summary-stat" key={String(name)}><div className="stat-top"><Text type="secondary">{name}</Text>{icon}</div><strong>{value}</strong><Text type="secondary">{note}</Text></div>)}
    </section>
    <div className="overview-columns">
      <Card title="开始一次分类工作流" className="workflow-card"><Paragraph type="secondary">按顺序完成数据准备、模型验证与大批量处理。</Paragraph>
        {[
          { title: '整理图像与标签', note: '导入类别文件夹，确认样本与来源分组。', page: 'assets', done: labeled > 0, icon: <FolderOpenOutlined /> },
          { title: '冻结数据，训练模型', note: '独立保留验证与测试数据，比较分类效果。', page: 'train', done: !!project.model_count, icon: <ExperimentOutlined /> },
          { title: '批量分类与人工复核', note: '跟踪任务、导出结果，将纠错纳入下一版数据。', page: 'jobs', done: false, icon: <ThunderboltOutlined /> },
        ].map((step, index) => <button className="workflow-step" key={step.page} onClick={() => navigate(step.page as Page)}><span className={`step-icon ${step.done ? 'done' : ''}`}>{step.done ? <CheckCircleOutlined /> : step.icon}</span><span className="step-text"><span className="step-title"><small>0{index + 1}</small>{step.title}</span><span>{step.note}</span></span><ArrowRightOutlined /></button>)}
      </Card>
      <Card title="类别分布" extra={<Button type="link" onClick={() => navigate('assets')}>管理样本</Button>}>
        {total ? <div className="class-distribution">{project.labels.map((label, i) => {
          const count = counts.find(item => item.label_id === label.label_id)?.count ?? 0;
          return <div key={label.label_id}><div className="distribution-label"><span><i style={{ background: [clearTheme.link, '#667c68', '#a89d64', '#807490'][i % 4] }} />{label.display_name}</span><Text type="secondary">{count} 张</Text></div><div className="distribution-track"><span style={{ width: `${count / Math.max(total, 1) * 100}%`, background: [clearTheme.link, '#667c68', '#a89d64', '#807490'][i % 4] }} /></div></div>;
        })}<Divider /><div className="distribution-label"><Text type="secondary">未标注图片</Text><strong>{total - labeled} 张</strong></div></div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="导入图片后查看类别分布" />}
      </Card>
    </div>
    <div className="principle-strip"><DatabaseOutlined /><div><strong>每次迭代，都有据可查</strong><span>样本划分、预处理、模型权重与分类结果按版本保存。人工修改不会覆盖旧实验。</span></div></div>
  </>;
}
