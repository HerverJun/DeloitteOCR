export type DiagnosticsResponse = {
  passed: boolean;
  details: string;
  free_bytes?: number;
  error?: string;
};

type DoctorReport = {
  platform?: string;
  python?: string;
  bundle?: string;
  gpu?: string;
  gpu_exit?: number;
  disk_free_bytes?: number;
  missing?: string[];
  runtimes?: Record<string, boolean>;
};

export function parseDoctorReport(details: string): DoctorReport | null {
  try {
    const value: unknown = JSON.parse(details);
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const row = value as Record<string, unknown>;
    if (typeof row.gpu_exit !== "number" || !Array.isArray(row.missing) || !row.missing.every(item => typeof item === "string") ||
        !row.runtimes || typeof row.runtimes !== "object" || Array.isArray(row.runtimes) ||
        !Object.keys(row.runtimes).length || !Object.values(row.runtimes).every(item => typeof item === "boolean")) return null;
    return row as DoctorReport;
  } catch {
    return null;
  }
}

export function formatCapacity(bytes: number | undefined): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "未获取";
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

export function gpuDescription(value: string | undefined): string {
  if (!value?.trim()) return "未获取显卡信息";
  const [name, driver, total, free] = value.trim().split(",").map(part => part.trim());
  if (!name || !driver || !total || !free) return value.trim();
  return `${name} · 驱动 ${driver} · 显存空闲 ${free} / ${total}`;
}
