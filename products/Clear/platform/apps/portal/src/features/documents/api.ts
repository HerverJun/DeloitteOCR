import { API, post, request, type Asset } from '../batches/api'

export type DocumentBlock = { block_id: string; type: string; text: string; order: number; table_id?: string; bbox?: number[] | null; source?: unknown }
export type DocumentPage = { page_number: number; width?: number | null; height?: number | null; needs_ocr?: boolean; blocks: DocumentBlock[] }
export type DocumentRun = { document_id: string; status: string; stage?: string; revision?: number; result_hash?: string | null; error?: string | null; created_at?: string;
  source?: { original_name?: string; asset_id: string; asset_revision: number; blob_hash?: string }; asset_id?: string; original_name?: string;
  document?: { schema_version: string; revision: number; pagination_kind?: 'logical' | 'slide' | 'physical'; source: unknown; engine: unknown; pages: DocumentPage[]; warnings?: unknown[] } | null }
export type DocumentCell = { row: number; column: number; row_span?: number; column_span?: number; text: string; polygon?: number[][] | null; [key: string]: unknown }
export type DocumentTable = { table_id: string; revision: number; rows: number; columns: number; cells: DocumentCell[] }
export const documentsBase = (workspace: string) => `/workspaces/${encodeURIComponent(workspace)}/documents`
export const documentsUrl = (workspace: string) => `${API}${documentsBase(workspace)}`
export const parseableDocument = (name: string) => /\.(pdf|jpe?g|png|bmp|tiff?|webp|docx|pptx|ofd)$/i.test(name)
export const documentsApi = {
  list: (workspace: string, offset = 0, limit = 5) => request<{ items: DocumentRun[]; total: number; next_offset?: number | null }>(`${documentsBase(workspace)}?offset=${offset}&limit=${limit}`),
  get: (workspace: string, id: string) => request<DocumentRun>(`${documentsBase(workspace)}/${encodeURIComponent(id)}`),
  parse: (workspace: string, asset: Asset, requestId: string) => post<DocumentRun>(`${documentsBase(workspace)}/parse`, {
    schema_version: '1.0', request_id: requestId, asset_id: asset.asset_id, asset_revision: asset.revision, blob_hash: asset.blob_hash, ocr_policy: 'auto',
  }),
  page: (workspace: string, id: string, page: number, offset: number) => request<{ blocks: DocumentBlock[]; total: number }>(`${documentsBase(workspace)}/${encodeURIComponent(id)}/pages/${page}?offset=${offset}&limit=30`),
  table: (workspace: string, id: string, table: string) => request<DocumentTable>(`${documentsBase(workspace)}/${encodeURIComponent(id)}/tables/${encodeURIComponent(table)}`),
  review: (workspace: string, id: string, table: DocumentTable, cells: DocumentCell[]) => post<DocumentTable>(`${documentsBase(workspace)}/${encodeURIComponent(id)}/tables/${encodeURIComponent(table.table_id)}/review`, {
    expected_revision: table.revision, cells: cells.map(({ row, column, row_span, column_span, text, confidence, polygon, is_header }) => ({ row, column, row_span: row_span || 1, column_span: column_span || 1, text, confidence: confidence ?? null, polygon: polygon ?? null, is_header: is_header ?? false })),
  }),
  control: (workspace: string, id: string, action: 'cancel' | 'resume') => post<DocumentRun>(`${documentsBase(workspace)}/${encodeURIComponent(id)}/control/${action}`, {}),
}
