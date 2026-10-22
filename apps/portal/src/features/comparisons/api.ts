import { API, post, request, type Asset } from '../batches/api'

export type ComparisonSide = 'left' | 'right'
export type ProviderConnection = {
  id: string; version: number; name: string; protocol: 'openai_chat_completions' | 'anthropic_messages'
  base_url: string; model: string; has_key: boolean; context_window: number; max_output_tokens: number
  supports_vision: boolean; structured_output: 'prompt' | 'json_object' | 'json_schema'; max_image_edge?: number; image_max_bytes?: number; vision_tokens_per_tile?: number; max_request_bytes?: number; total_seconds?: number
}
export type LayoutParseConfig = {
  mode: 'local_ocr' | 'multimodal'; engine: 'paddlevl' | 'mineru_pro'; provider_id: string | null; provider_version: number | null
  max_image_edge: number; image_max_bytes: number; max_output_tokens: number; vision_tokens_per_tile: number; fallback_local: boolean
}
export const defaultLayout: LayoutParseConfig = { mode: 'local_ocr', engine: 'paddlevl', provider_id: null, provider_version: null,
  max_image_edge: 2560, image_max_bytes: 8388608, max_output_tokens: 8192, vision_tokens_per_tile: 512, fallback_local: false }
export type ComparisonField = { key: string; label: string; type: 'text' | 'date' | 'number' | 'percentage'; aliases: string[]; tolerance: number }
export type ComparisonProfile = { id: string; version: number; name: string; instructions: string; fields: ComparisonField[];
  entity_aliases: Record<string, string[]>; date_policy: 'same_as_of' | 'ignore_as_of'; as_of_a: string | null; as_of_b: string | null }
export const defaultProfile: ComparisonProfile = { id: 'project_progress', version: 1, name: '项目进度', instructions: '比较两侧项目进度相关事实',
  date_policy: 'same_as_of', as_of_a: null, as_of_b: null, entity_aliases: {},
  fields: [['project', '项目', 'text'], ['work_item', '工作项', 'text'], ['milestone', '里程碑', 'text'], ['owner', '负责人', 'text'],
    ['planned_date', '计划日期', 'date'], ['actual_date', '实际日期', 'date'], ['completion', '完成率', 'percentage'], ['status', '状态', 'text'],
    ['risk', '风险', 'text'], ['dependency', '依赖', 'text']].map(([key, label, type]) => ({ key, label, type: type as ComparisonField['type'], aliases: [], tolerance: 0 })) }
export type AssetReference = { asset_id: string; asset_revision: number; blob_hash: string }
export type ComparisonInput = { assets: AssetReference[]; layout: LayoutParseConfig }
export type ComparisonCreate = { request_id: string; title: string; left: ComparisonInput; right: ComparisonInput; profile_id: string;
  profile_version: number; comparison_provider_id: string; comparison_provider_version: number }
export type ComparisonBlock = { block_id: string; type: string; text: string; order?: number; bbox?: number[] | null; coordinate_kind?: string; table?: unknown; [key: string]: unknown }
export type ComparisonPage = { page_number: number; status?: string; engine?: string; error?: string | null; revision?: number; blocks: ComparisonBlock[];
  raw_response?: unknown; image_url?: string; original_url?: string; [key: string]: unknown }
export type ComparisonEvidence = { side?: string; page_number?: number; block_id?: string; quote?: string; text?: string; [key: string]: unknown }
export type ComparisonItem = { id: string; field: string; entity: string; status: string; a_value: unknown; b_value: unknown; explanation: string;
  evidence?: ComparisonEvidence[]; [key: string]: unknown }
export type ComparisonRun = { comparison_id: string; title: string; status: string; stage: string; revision: number; created_at?: string; error?: string | null;
  progress?: { total: number; processed: number; succeeded: number; failed: number; current_side?: string; current_page?: number };
  left?: { document?: { pages: ComparisonPage[]; revision?: number }; pages?: ComparisonPage[]; layout?: LayoutParseConfig };
  right?: { document?: { pages: ComparisonPage[]; revision?: number }; pages?: ComparisonPage[]; layout?: LayoutParseConfig };
  result?: { summary?: unknown; items: ComparisonItem[]; [key: string]: unknown } | null; files?: string[]; outdated?: boolean; profile?: ComparisonProfile; comparison_provider?: ProviderConnection }
export const comparisonsBase = (workspace: string) => `/workspaces/${encodeURIComponent(workspace)}/comparisons`
export const comparisonFile = (workspace: string, id: string, name: string, revision?: number) => `${API}${comparisonsBase(workspace)}/${encodeURIComponent(id)}/files/${encodeURIComponent(name)}${revision ? `?revision=${revision}` : ''}`
export const activeComparison = (status: string) => ['queued', 'running', 'cancelling', 'waiting_resource'].includes(status)
export const comparisonLabels: Record<string, string> = { consistent: '一致', different: '存在差异', only_a: '仅 A 提及', only_b: '仅 B 提及', insufficient: '信息不足' }
export const comparisonStages: Record<string, string> = { queued: '等待处理', preparing: '准备页面', rendering: '渲染页面', parsing: '逐页解析', parsing_left: '解析 A 侧', parsing_right: '解析 B 侧', extracting: '提取主题事实', comparing: '对照分析', reporting: '生成固定报告', completed: '处理完成', done: '处理完成' }
export const fileLabels: Record<string, string> = { 'report.html': 'HTML 报告', 'comparison.json': '比较 JSON', 'left.md': 'A 侧 Markdown', 'left.json': 'A 侧 JSON', 'right.md': 'B 侧 Markdown', 'right.json': 'B 侧 JSON', 'manifest.json': '来源清单' }
export const assetReference = (asset: Asset): AssetReference => ({ asset_id: asset.asset_id, asset_revision: asset.revision, blob_hash: asset.blob_hash })
export function inputError(left: Asset[], right: Asset[]) {
  if (left.length !== 1 || !/\.pdf$/i.test(left[0].original_name)) return 'A 侧请选择一份 PDF。'
  if (!right.length) return 'B 侧请选择一份 PPTX 或一组幻灯片图片。'
  if (right.length === 1 && /\.pptx$/i.test(right[0].original_name)) return ''
  if (right.length > 100) return '幻灯片图片最多 100 张。'
  if (!right.every(asset => /\.(png|jpe?g|webp|bmp|tiff?)$/i.test(asset.original_name))) return 'B 侧应为单个 PPTX 或全部为图片，不能混合。'
  return ''
}
export function moveAsset(ids: string[], index: number, direction: -1 | 1) {
  const next = [...ids], target = index + direction
  if (target < 0 || target >= next.length) return next
  ;[next[index], next[target]] = [next[target], next[index]]
  return next
}
export function profileError(profile: ComparisonProfile) {
  if (!profile.name.trim() || !profile.instructions.trim()) return '请填写配置名称和比较要求。'
  if (!profile.fields.length) return '至少保留一个比较字段。'
  if (profile.fields.some(field => !/^[a-z][a-z0-9_]{0,63}$/.test(field.key) || !field.label.trim() || field.tolerance < 0)) return '字段标识需以小写字母开头，仅含小写字母、数字和下划线，最多 64 字符；名称必填，容差不能为负。'
  if (new Set(profile.fields.map(field => field.key)).size !== profile.fields.length) return '字段标识不能重复。'
  return ''
}
export const comparisonsApi = {
  list: (workspace: string, offset = 0, limit = 20) => request<{ items: ComparisonRun[]; total: number }>(`${comparisonsBase(workspace)}?offset=${offset}&limit=${limit}`),
  get: (workspace: string, id: string, revision?: number) => request<ComparisonRun>(`${comparisonsBase(workspace)}/${encodeURIComponent(id)}${revision ? `?revision=${revision}` : ''}`),
  create: (workspace: string, payload: ComparisonCreate) => post<ComparisonRun>(comparisonsBase(workspace), payload),
  control: (workspace: string, id: string, action: 'cancel' | 'resume') => post<ComparisonRun>(`${comparisonsBase(workspace)}/${encodeURIComponent(id)}/control/${action}`, {}),
  rerun: (workspace: string, id: string, side: ComparisonSide, pages: number[], revision: number, layout?: LayoutParseConfig) => post<ComparisonRun>(`${comparisonsBase(workspace)}/${encodeURIComponent(id)}/rerun-pages`, { side, pages, expected_revision: revision, ...(layout ? { layout } : {}) }),
  recompare: (workspace: string, id: string, revision: number, profile: ComparisonProfile, provider: ProviderConnection) => post<ComparisonRun>(`${comparisonsBase(workspace)}/${encodeURIComponent(id)}/recompare`, {
    expected_revision: revision, profile_id: profile.id, profile_version: profile.version, comparison_provider_id: provider.id, comparison_provider_version: provider.version }),
  page: (workspace: string, id: string, side: ComparisonSide, page: number, revision?: number) => request<ComparisonPage>(`${comparisonsBase(workspace)}/${encodeURIComponent(id)}/pages/${side}/${page}${revision ? `?revision=${revision}` : ''}`),
  review: (workspace: string, id: string, side: ComparisonSide, page: number, revision: number, blocks: ComparisonBlock[]) => post<ComparisonRun>(`${comparisonsBase(workspace)}/${encodeURIComponent(id)}/pages/${side}/${page}/review`, { expected_revision: revision, blocks }),
  profiles: (workspace: string) => request<{ items: ComparisonProfile[] }>(`${comparisonsBase(workspace)}/profiles`),
  saveProfile: (workspace: string, profile: ComparisonProfile, isNew = false) => { const { version, ...values } = profile; return post<ComparisonProfile>(`${comparisonsBase(workspace)}/profiles`, isNew ? values : { ...values, version }) },
  suggest: (workspace: string, instructions: string, provider: ProviderConnection) => post<ComparisonProfile>(`${comparisonsBase(workspace)}/profiles/suggest`, { instructions, provider_id: provider.id, provider_version: provider.version }),
  connections: (workspace: string) => request<{ items: ProviderConnection[] }>(`${comparisonsBase(workspace)}/connections`),
  saveConnection: (workspace: string, connection: Partial<ProviderConnection> & { api_key?: string }) => post<ProviderConnection>(`${comparisonsBase(workspace)}/connections`, connection),
  probe: (workspace: string, id: string, version?: number) => post<{ passed: boolean; vision_tested?: boolean; message?: string; detail?: string; [key: string]: unknown }>(`${comparisonsBase(workspace)}/connections/${encodeURIComponent(id)}/probe`, { version }),
}
