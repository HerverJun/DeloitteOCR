import { useEffect, useState } from "react";
import { request } from "./api";
import type { Version } from "./types";
import "./geometry.css";

export type Location = { level: string; polygon: number[][] | null; version_id: string; reason: string; range_semantics?: string };
export function RegionPreview({ version, location, tablePolygon, onReady }: {
  version: Version; location: Location; tablePolygon?: number[][] | null; onReady?: (ready: boolean) => void;
}) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [scope, setScope] = useState("local");
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let active = true, objectUrl = "";
    setUrl(""); setError(""); setLoaded(false);
    request(`/versions/${version.id}/image`).then(r => r.blob()).then(blob => {
      objectUrl = URL.createObjectURL(blob);
      if (active) setUrl(objectUrl); else URL.revokeObjectURL(objectUrl);
    }).catch(e => { if (active) setError(String(e)); });
    return () => { active = false; if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [version.id]);
  useEffect(() => { onReady?.(loaded); return () => onReady?.(false); }, [loaded, location, onReady]);
  const valid = (p?: number[][] | null) => p && p.length >= 3 && p.every(v => v.length === 2 && v.every(Number.isFinite) && v[0] >= 0 && v[1] >= 0 && v[0] <= version.width && v[1] <= version.height) ? p : null;
  const polygon = location.version_id === version.id ? valid(location.polygon) : null;
  const crop = scope === "full" ? null : scope === "table" ? valid(tablePolygon) : polygon;
  let box = [0, 0, version.width, version.height];
  if (crop) {
    const x = Math.min(...crop.map(p => p[0])), y = Math.min(...crop.map(p => p[1]));
    const w = Math.max(...crop.map(p => p[0])) - x, h = Math.max(...crop.map(p => p[1])) - y;
    const pad = Math.max(10, Math.min(w, h) * .2);
    box = [Math.max(0, x-pad), Math.max(0, y-pad), Math.min(version.width, x+w+pad)-Math.max(0, x-pad), Math.min(version.height, y+h+pad)-Math.max(0, y-pad)];
  }
  return <figure className="region-preview">
    <div className="geometry-actions"><strong>原图核对</strong><select aria-label="原图预览范围" value={scope} onChange={e => setScope(e.target.value)}>
      <option value="local">{location.range_semantics === "text_extent" ? "文字范围" : location.level === "cell" ? "单元格" : "局部"}</option>{tablePolygon && <option value="table">整表</option>}<option value="full">全图</option>
    </select></div>
    {error ? <p role="alert">{error}</p> : url ? <svg role="img" aria-label="疑点局部原图" viewBox={box.join(" ")}>
      <image href={url} width={version.width} height={version.height} onLoad={() => setLoaded(true)} />
      {polygon && <polygon points={polygon.map(p => p.join(",")).join(" ")} fill="none" stroke="#147d64" strokeWidth="2" vectorEffect="non-scaling-stroke" />}
    </svg> : <p>正在载入原图…</p>}
    <figcaption>{location.reason}{!polygon && " · 无可靠局部定位，显示全图"}</figcaption>
  </figure>;
}
