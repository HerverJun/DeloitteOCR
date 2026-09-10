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
  rows: number;
  columns: number;
  caption?: string;
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
  kind?: string;
  result_version_id?: string | null;
};
export type ProjectState = {
  project: Project;
  images: Photo[];
  versions: Version[];
  tasks: Task[];
  queue: { task_id: string | null; engine: string | null; loaded: boolean };
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
