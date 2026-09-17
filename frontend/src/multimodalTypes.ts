import type { GeometryTarget } from "./GeometryPanel";
import type { Location } from "./RegionPreview";

export type MultimodalModel = {
  id: string;
  label: string;
  available: boolean;
  reason?: string;
};
export type MultimodalCatalog = {
  models: MultimodalModel[];
  default_model: string | null;
  reason?: string;
  policy?: Record<string, unknown>;
};
export type MultimodalRequest = {
  task_id: string;
  request_id?: string;
  status: string;
  phase?: string;
  error?: string | null;
  model_id: string;
  created: string;
  scope: "page" | "target";
  snapshot_current?: boolean;
};
export type MultimodalProposal = {
  id: string;
  task_id: string;
  target_id: string;
  target: GeometryTarget;
  before: string;
  after: string | null;
  decision: "keep" | "replace" | "uncertain";
  reason: string;
  state: "pending" | "accepted" | "rejected" | "question" | "stale";
  evidence: Location & { table_polygon?: number[][] | null };
  model_id?: string;
  revision?: number;
  version_id?: string;
};
export type MultimodalView = {
  requests: MultimodalRequest[];
  proposals: MultimodalProposal[];
  counts: Record<string, number>;
  revision: number;
};
export type MultimodalSubmission = {
  revision: number;
  version_id: string;
  model_id: string;
  scope: "page" | "target";
  target?: GeometryTarget;
  request_id: string;
};
export type MultimodalDecision = {
  action: "accept" | "reject" | "question";
  revision: number;
  version_id: string;
  request_id: string;
};
export type PendingMultimodalRequest = {
  result_id: string;
  version_id: string;
} & (
  | { kind: "submit"; body: MultimodalSubmission }
  | { kind: "decision"; proposal_id: string; body: MultimodalDecision }
);

export function multimodalTargetLabel(target: GeometryTarget) {
  return target.kind === "cell"
    ? `表 ${target.table + 1} · 第 ${target.row + 1} 行 / 第 ${target.column + 1} 列`
    : `文字第 ${target.start + 1}–${target.end} 字符`;
}

const recoveryKey = (resultId: string, versionId: string) =>
  `ocr-multimodal-pending:${encodeURIComponent(resultId)}:${encodeURIComponent(versionId)}`;

export function readMultimodalPending(resultId: string, versionId: string): PendingMultimodalRequest | null {
  try {
    const value = JSON.parse(sessionStorage.getItem(recoveryKey(resultId, versionId)) || "null") as PendingMultimodalRequest | null;
    if (!value || value.result_id !== resultId || value.version_id !== versionId ||
      value.body?.version_id !== versionId || !Number.isInteger(value.body?.revision) ||
      typeof value.body?.request_id !== "string" || !value.body.request_id) return null;
    if (value.kind === "submit" && typeof value.body.model_id === "string" &&
      (value.body.scope === "page" || (value.body.scope === "target" && value.body.target))) return value;
    if (value.kind === "decision" && typeof value.proposal_id === "string" &&
      ["accept", "reject", "question"].includes(value.body.action)) return value;
    return null;
  } catch { return null; }
}

export function writeMultimodalPending(resultId: string, versionId: string, value: PendingMultimodalRequest | null) {
  if (value && (value.result_id !== resultId || value.version_id !== versionId || value.body.version_id !== versionId))
    throw new Error("审校请求与当前页面不匹配");
  try {
    const key = recoveryKey(resultId, versionId);
    if (value) sessionStorage.setItem(key, JSON.stringify(value));
    else sessionStorage.removeItem(key);
  } catch { /* Keep the same request in memory when browser storage is unavailable. */ }
}
