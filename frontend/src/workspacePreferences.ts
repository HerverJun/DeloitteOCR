import { useEffect, useState } from "react";
import type { Photo, Task } from "./types";

export function readPreference<T>(
  key: string,
  fallback: T,
  valid: (v: unknown) => v is T,
): T {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) || "null");
    return valid(value) ? value : fallback;
  } catch {
    return fallback;
  }
}
export function usePreference<T>(
  key: string,
  fallback: T,
  valid: (v: unknown) => v is T,
) {
  const [value, setValue] = useState<T>(() =>
    readPreference(key, fallback, valid),
  );
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch {
      /* Read-only browser storage is optional. */
    }
  }, [key, value]);
  return [value, setValue] as const;
}
export const validMode = (v: unknown): v is string =>
  typeof v === "string" &&
  ["text", "handwriting", "table", "compare"].includes(v);
export const validEngine = (v: unknown): v is string =>
  typeof v === "string" &&
  ["ppocr", "paddlevl", "glm", "hunyuan", "all"].includes(v);
export function imageStatus(id: string, tasks: Task[]): string {
  const relevant = tasks.filter(
    (t) => t.image_id === id && t.kind !== "dewarp" && t.engine !== "dewarp",
  );
  if (relevant.some((t) => ["queued", "running"].includes(t.status)))
    return "processing";
  return relevant.at(-1)?.status || "unrecognized";
}
export function filterPhotos(
  photos: Photo[],
  tasks: Task[],
  search: string,
  status: string,
) {
  return photos.filter(
    (p) =>
      p.name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()) &&
      (status === "all" || imageStatus(p.id, tasks) === status),
  );
}
export function toggleVisible(
  selected: string[],
  visible: string[],
  checked: boolean,
) {
  return checked
    ? [...new Set([...selected, ...visible])]
    : selected.filter((id) => !visible.includes(id));
}
