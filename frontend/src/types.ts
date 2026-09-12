export type Cell = {
  row: number;
  column: number;
  row_span: number;
  column_span: number;
  text: string;
  confidence?: number | null;
  polygon?: number[][] | null;
};
export type Table = {
  fusion_id?: string;
  rows: number;
  columns: number;
  caption?: string;
  source?: { start: number; end: number; format: string; sha256: string };
  cells: Cell[];
};
export type Edit = { text: string; tables: Table[] };
export type Block = {
  text: string;
  kind: string;
  confidence: number | null;
  polygon: number[][] | null;
};
export type Result = {
  id: string;
  task_id: string;
  revision: number;
  cursor: number;
  can_undo: boolean;
  can_redo: boolean;
  edited: Edit;
  original: {
    origin?: "single-engine" | "fusion";
    fusion?: {
      session_id: string;
      policy: { version: string; content_type: string; mode: string; automatic_replacement: boolean };
      policy_sha256: string;
      expected_sources: string[];
      valid_sources: string[];
      coverage: number;
      unresolved: number;
      baseline: string;
      structure_fallbacks?: { table: number; engine: string }[];
      evidence_units: number;
    };
    warnings?: { code: string; stage: string; message: string }[];
    engine: string;
    text: string;
    tables: Table[];
    blocks: Block[];
    elapsed_seconds: number;
    load_seconds: number;
    project_image_version?: string;
    image: { width: number; height: number };
    engine_session?: { id: string; pid: number; load_seconds: number };
  };
};
export type Project = {
  id: string;
  name: string;
  created: string;
  updated: string;
};
export type Photo = {
  id: string;
  name: string;
  active_version: string;
  selected_result: string | null;
  review_status?: "pending" | "confirmed" | "question";
  review_state?: {
    result_id: string | null;
    revision: number | null;
    version_id: string | null;
    status: string;
    stale: boolean;
  };
  project_id: string;
};
export type Version = {
  id: string;
  image_id: string;
  parent_id: string | null;
  width: number;
  height: number;
  operations: string;
  created: string;
};
export type Task = {
  id: string;
  image_id: string;
  version_id: string;
  engine: string;
  status: string;
  phase: string;
  error: string | null;
  result_id: string | null;
  created: string;
  batch?: string;
  batch_id?: string;
  preprocess?: string;
  input_version_id?: string;
  engine_package?: string;
  kind?: string;
  result_version_id?: string | null;
};
export type ProjectState = {
  revision?: number;
  disk?: { low_space: boolean; warning: string | null; free_bytes: number };
  project: Project;
  images: Photo[];
  versions: Version[];
  tasks: Task[];
  fusion_queue?: { healthy?: boolean; alive?: boolean; last_error?: string | { message: string } | null };
  queue: {
    task_id: string | null;
    engine: string | null;
    loaded: boolean;
    alive?: boolean;
    healthy?: boolean;
    state?: string;
    last_error?: string | { message: string } | null;
    consecutive_failures?: number;
  };
};
export type Engine = {
  name: string;
  capabilities: {
    tables: boolean;
    text_coordinates?: boolean;
    region_coordinates?: boolean;
    confidence: boolean;
  };
};
export const engineNames: Record<string, string> = {
  ppocr: "PP-OCRv6",
  paddlevl: "PaddleOCR-VL",
  glm: "GLM-OCR",
  hunyuan: "HunyuanOCR",
  dewarp: "UVDoc 去弯曲",
  fusion: "融合草稿",
};

export type ReviewIssue = {
  id: string;
  basis: string;
  category: string;
  state: "pending" | "resolved" | "question" | "stale";
  current_value: string | Table | null;
  current_fingerprint: string;
  baseline: string | Table | null;
  candidates: { id: string; value: string | Table | null; sources: string[]; weight: number }[];
  target: { kind: string; table_id?: string; row?: number; column?: number; start?: number; end?: number; unlocatable?: boolean };
  source_states: Record<string, string>;
  reason: string;
  expected_sources: string[];
  valid_sources: string[];
  coverage: number;
  context_current: boolean;
  location: { level: "image" | "region" | "cell"; polygon: number[][] | null; version_id: string; reason: string };
};
export type ReviewPage = {
  result_id: string;
  revision: number;
  counts: Record<string, number>;
  total: number;
  offset: number;
  limit: number;
  issues: ReviewIssue[];
  context_current: boolean;
  position: string | null;
};
export const statuses: Record<string, string> = {
  queued: "等待识别",
  running: "识别中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  paused: "已暂停",
  interrupted: "等待恢复",
};
