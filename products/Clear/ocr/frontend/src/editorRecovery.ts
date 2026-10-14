import type { Edit } from "./types";

export type Recovery = {
  resultId: string;
  revision: number;
  edit: Edit | null;
  review: { issueId: string; value: string } | null;
  decision: { resultId: string; issueId: string; body: Record<string, unknown> } | null;
  updated: number;
};
const prefix = "ocr-editor-recovery:";
const tabKey = "ocr-editor-client";
export function recoveryClient(): string {
  let value = sessionStorage.getItem(tabKey);
  if (!value) { value = crypto.randomUUID(); sessionStorage.setItem(tabKey, value); }
  return value;
}
export function writeRecovery(client: string, recovery: Recovery) {
  const key = `${prefix}${recovery.resultId}:${client}`;
  if (!recovery.edit && !recovery.review && !recovery.decision) localStorage.removeItem(key);
  else localStorage.setItem(key, JSON.stringify(recovery));
}
export function readRecovery(client: string, resultId: string): Recovery | null {
  const ownKey = `${prefix}${resultId}:${client}`;
  const own = localStorage.getItem(ownKey);
  const keys = own ? [ownKey] : Object.keys(localStorage).filter(key => key.startsWith(`${prefix}${resultId}:`));
  const candidates: { item: Recovery; key: string; raw: string }[] = [];
  for (const key of keys) {
    const value = localStorage.getItem(key);
    if (!value) continue;
    try {
      const item = JSON.parse(value) as Recovery;
      if (item.resultId === resultId && Number.isInteger(item.revision) && typeof item.updated === "number") candidates.push({ item, key, raw: value });
    } catch { /* A damaged local entry must not overwrite server content. */ }
  }
  const selected = candidates.sort((a,b) => b.item.updated-a.item.updated)[0];
  if (!selected) return null;
  if (selected.key !== ownKey) {
    // Transfer the exact snapshot before acknowledging it. A newer draft from
    // the original window must survive; saving here must not resurrect an old one.
    localStorage.setItem(ownKey, selected.raw);
    if (localStorage.getItem(selected.key) === selected.raw) localStorage.removeItem(selected.key);
  }
  return selected.item;
}
