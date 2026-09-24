function describeOperation(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return "其他处理";
  const operation = value as Record<string, unknown>;
  switch (operation.kind) {
    case "import": return operation.exif_normalized ? "导入（已校正照片方向）" : "导入原图";
    case "rotate": return typeof operation.degrees === "number" ? `旋转 ${operation.degrees}°` : "旋转";
    case "crop": return "裁剪";
    case "perspective": return "透视校正";
    case "contrast": return typeof operation.factor === "number" ? `对比度 ${operation.factor} 倍` : "对比度增强";
    case "batch": {
      const preset = Array.isArray(operation.preset) ? operation.preset.map(describeOperation) : [];
      return preset.length ? `批次处理：${preset.join("、")}` : "批次处理";
    }
    default: return "其他处理";
  }
}

export function operationSummary(value: string | undefined): string {
  if (!value) return "未记录";
  try {
    const data: unknown = JSON.parse(value);
    if (Array.isArray(data)) return data.length ? data.map(describeOperation).join("、") : "无";
    return describeOperation(data);
  } catch {
    return "处理记录无法读取";
  }
}
