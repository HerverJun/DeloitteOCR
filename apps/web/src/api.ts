export type Label = { label_id: string; display_name: string; definition: string };
export type Project = {
  id: string; name: string; description: string; labels: Label[]; active_model_id: string | null;
  asset_count?: number; model_count?: number; dataset_count?: number;
  asset_counts?: { label_id: string | null; count: number }[];
  job_counts?: { state: string; count: number }[];
};
export type Asset = { id: string; name: string; label_id: string | null; path: string; sha256: string; width: number; height: number; group_id: string; source_id: string };
export type Dataset = { id: string; name: string; created_at: string; stats: { holdout_policy?: string; total: number; splits: Record<string, number>; classes: Record<string, Record<string, number>> } };
export type Job = { id: string; kind: 'import' | 'train' | 'inference'; state: string; stage: string; total: number; done: number; failed: number; created_at: string; error: string | null; result: Record<string, unknown> | null };
export type Model = { id: string; name: string; created_at: string; metadata: { encoder: { model_name: string }; input: { height: number }; labels: (Label & { class_index: number })[]; decision_policy: { thresholds_by_label_id: Record<string, number> } }; metrics: { validation: { macro_f1?: number; accuracy: number; sample_count: number; confusion_matrix: number[][] }; test: { macro_f1?: number; coverage?: number; accepted_precision?: number | null; accuracy_ci95_wilson?: number[]; accepted_precision_ci95_wilson?: number[] | null; accuracy: number; sample_count: number; confusion_matrix: number[][]; classification_report: Record<string, { precision: number; recall: number; 'f1-score': number; support: number }> }; export: { max_abs_error: number; node_provider_events: Record<string, number> }; warning: string } };
export type Result = { id: string; asset_id: string | null; name: string; path: string; state: string; confirmed_label: string | null; error: string | null; prediction: { label_id: string; display_name: string; score: number; status: string } | null };
export type Runtime = { python: string; data_dir: string; free_bytes: number; packages: Record<string, string | null>; gpu: { name: string; memory_total_mib: number; memory_used_mib: number; utilization: number } | null };

export const base = '/api/v1';

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  headers.set('X-Platform-Request', '1');
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  const response = await fetch(base + path, { ...options, headers });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = typeof error.detail === 'string' ? error.detail : JSON.stringify(error.detail ?? `请求失败 ${response.status}`);
    throw new Error(detail);
  }
  return response.json();
}

export function post<T>(path: string, body: unknown): Promise<T> {
  return api(path, { method: 'POST', body: JSON.stringify(body) });
}

export const stateLabels: Record<string, string> = {
  queued: '等待执行', running: '运行中', pausing: '正在暂停', paused: '已暂停',
  cancelling: '正在取消', cancelled: '已取消', succeeded: '已完成',
  completed_with_errors: '完成，有错误', failed: '失败', pending: '待处理',
  accepted: '已接受', needs_review: '待复核',
};
export const kindLabels = { import: '导入图片', train: '模型训练', inference: '批量分类' };
export const activeStates = ['queued', 'running', 'pausing', 'cancelling'];
export const dateText = (text: string) => new Date(text).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
