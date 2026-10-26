import { Alert, Button, Space, Typography } from 'antd';
import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { type Runtime } from '../api';
import { readRuntime, verifyDevice } from '../runtimeRequests';

export function useDeviceReadiness(enabled = true, device: 'cpu' | 'cuda' = 'cuda') {
  const client = useQueryClient();
  const key = ['runtime-devices', device];
  const [refreshing, setRefreshing] = useState(false), [actionError, setActionError] = useState<unknown>();
  const action = useRef<AbortController | undefined>(undefined);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; setRefreshing(false); setActionError(undefined); return () => { mounted.current = false; action.current?.abort(new DOMException('设备检测已取消', 'AbortError')); }; }, [enabled, device]);
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => readRuntime({ signal, includeDevices: device === 'cuda', timeoutMs: device === 'cpu' ? 10000 : undefined }), enabled, staleTime: 30000, retry: false });
  const begin = () => { action.current?.abort(new DOMException('已改为新的设备检测', 'AbortError')); const controller = new AbortController(); action.current = controller; setRefreshing(false); setActionError(undefined); return controller; };
  const verify = async (kind: 'training' | 'inference') => {
    const controller = begin();
    try { await verifyDevice(kind, device, controller.signal); }
    catch (error) { if (mounted.current && action.current === controller && !(error instanceof DOMException && error.name === 'AbortError')) setActionError(error); throw error; }
    finally { if (action.current === controller) action.current = undefined; }
  };
  const refreshDevices = async () => {
    const controller = begin(); setRefreshing(true);
    try {
      await client.cancelQueries({ queryKey: key, exact: true });
      const value = await readRuntime({ signal: controller.signal, refresh: device === 'cuda', includeDevices: device === 'cuda', timeoutMs: device === 'cpu' ? 10000 : undefined });
      if (mounted.current && action.current === controller) client.setQueryData(key, value);
    } catch (error) { if (mounted.current && action.current === controller && !(error instanceof DOMException && error.name === 'AbortError')) setActionError(error); }
    finally { if (mounted.current && action.current === controller) setRefreshing(false); if (action.current === controller) action.current = undefined; }
  };
  return { ...query, error: actionError || query.error, isPending: refreshing || query.isPending && query.fetchStatus === 'fetching', refreshDevices, verifyDevice: verify };
}

export function DeviceReadiness({ runtime, kind, device, loading, error, refresh }: {
  runtime?: Runtime; kind: 'training' | 'inference'; device: 'cpu' | 'cuda'; loading: boolean; error: unknown; refresh: () => void;
}) {
  const state = runtime?.devices?.[kind][device];
  const other = runtime?.devices?.[kind][device === 'cuda' ? 'cpu' : 'cuda'];
  const exportState = kind === 'training' ? runtime?.devices?.inference[device] : undefined;
  return <Alert className="bottom-gap" showIcon type={state?.available === false ? 'warning' : state?.available ? 'success' : 'info'}
    message={loading ? '正在检测计算环境…' : error ? (error instanceof Error ? error.message : '设备检测暂不可用，请刷新后核实。') : state?.reason || (device === 'cpu' ? '已明确选择 CPU；提交前只检查 CPU 所需依赖。' : '设备状态尚未核实。')}
    description={<Space direction="vertical" size={2}>
      {other && <Typography.Text type="secondary">{device === 'cuda' ? 'CPU' : 'GPU'}：{other.reason}</Typography.Text>}
      {exportState && <Typography.Text type="secondary">训练后 ONNX 导出校验：{exportState.reason}</Typography.Text>}
      <Typography.Text type="secondary">训练使用 PyTorch，分类使用 ONNX Runtime，分别核实。检测不运行模型；执行失败会明确报错，设备不会自动切换。</Typography.Text>
      <Button size="small" disabled={loading} onClick={refresh}>重新检测设备</Button>
    </Space>} />;
}
