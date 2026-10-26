import { api, type Runtime } from './api';

export const RUNTIME_CHECK_TIMEOUT_MS = 60000;
const CPU_CHECK_TIMEOUT_MS = 10000;

export class RuntimeReadinessError extends Error {
  constructor(message: string, public readonly code: 'timeout' | 'unverified' | 'unavailable') {
    super(message); this.name = 'RuntimeReadinessError';
  }
}

type Options = { signal?: AbortSignal; refresh?: boolean; includeDevices?: boolean; timeoutMs?: number };
const cancelled = () => new DOMException('设备检测已取消', 'AbortError');

/** Bound the entire read, including a response whose JSON body never finishes. */
export async function readRuntime({ signal, refresh = false, includeDevices = true, timeoutMs = RUNTIME_CHECK_TIMEOUT_MS }: Options = {}): Promise<Runtime> {
  if (signal?.aborted) throw signal.reason || cancelled();
  const controller = new AbortController();
  const forwardAbort = () => controller.abort(signal?.reason || cancelled());
  signal?.addEventListener('abort', forwardAbort, { once: true });
  let rejectCancelled: () => void = () => {};
  const aborted = new Promise<never>((_, reject) => {
    rejectCancelled = () => reject(controller.signal.reason || cancelled());
    controller.signal.addEventListener('abort', rejectCancelled, { once: true });
  });
  const timeoutMessage = includeDevices ? '设备检测超时，状态尚未核实。请重试检测，或显式选择 CPU。' : 'CPU 依赖检查超时，状态尚未核实。请重试检测；本次未提交任务。';
  const timer = setTimeout(() => controller.abort(new RuntimeReadinessError(timeoutMessage, 'timeout')), timeoutMs);
  const path = refresh ? '/runtime?refresh_devices=true' : includeDevices ? '/runtime?include_devices=true' : '/runtime';
  try {
    return await Promise.race([api<Runtime>(path, { signal: controller.signal }), aborted]);
  } catch (error) {
    if (controller.signal.aborted) throw controller.signal.reason || cancelled();
    throw new RuntimeReadinessError('设备状态尚未核实，请重试检测。' + (error instanceof Error ? ` ${error.message}` : ''), 'unverified');
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener('abort', forwardAbort);
    controller.signal.removeEventListener('abort', rejectCancelled);
  }
}

export async function verifyDevice(kind: 'training' | 'inference', device: 'cpu' | 'cuda', signal?: AbortSignal) {
  // Explicit CPU work needs installed CPU dependencies, not a completed CUDA probe.
  const runtime = await readRuntime({ signal, includeDevices: device === 'cuda', timeoutMs: device === 'cpu' ? CPU_CHECK_TIMEOUT_MS : RUNTIME_CHECK_TIMEOUT_MS });
  if (device === 'cpu') {
    if (!runtime.packages) throw new RuntimeReadinessError('CPU 依赖状态尚未核实，请重试检测。', 'unverified');
    const hasOrt = !!(runtime.packages['onnxruntime-gpu'] || runtime.packages.onnxruntime);
    if (kind === 'training' && !runtime.packages.torch) throw new RuntimeReadinessError('PyTorch 未安装，暂不能提交 CPU 训练。', 'unavailable');
    if (!hasOrt) throw new RuntimeReadinessError('ONNX Runtime 未安装，暂不能进行 CPU 分类或训练后的导出校验。', 'unavailable');
    return;
  }
  const requireCuda = (framework: 'training' | 'inference') => {
    const readiness = runtime.devices?.[framework], state = readiness?.cuda;
    if (state?.available === false) throw new RuntimeReadinessError(state.reason + '。请更换设备或修复环境后再提交。', 'unavailable');
    // ORT provider registration is only a configured backend; its later model
    // initialization still fails explicitly rather than falling back to CPU.
    const configuredOrt = framework === 'inference' && readiness?.providers?.includes('CUDAExecutionProvider');
    if (state?.available !== true && !configuredOrt) throw new RuntimeReadinessError('GPU 设备状态尚未核实，请重试检测或显式选择 CPU；本次未提交任务。', 'unverified');
  };
  requireCuda(kind);
  if (kind === 'training') requireCuda('inference');
}
