import { useEffect, useRef, useState } from "react";
import { Button } from "@fluentui/react-components";
import { api } from "./api";
import { ResultDifference } from "./ResultComparison";
import { engineNames, type Result } from "./types";

type EnhanceRequest = { revision: number; request_id: string; box?: number[] };
type Comparison = { has_comparison: boolean; baseline?: Result; enhanced?: Result; box?: number[] | null; actual_engine?: string };

export function EnhancedRecognition({ result, disabled, beforeRun, currentResult, onAccepted, onError }: {
  result: Result; disabled: boolean; beforeRun: () => Promise<unknown>; currentResult: () => Result | null; onAccepted: () => Promise<unknown>; onError: (message: string) => void;
}) {
  const [scope, setScope] = useState("page"), [box, setBox] = useState([0, 0, result.original.image.width, result.original.image.height]);
  const [working, setWorking] = useState(false), [notice, setNotice] = useState("");
  const key = `clear-pro-enhance:${result.id}:${result.revision}`;
  const pending = useRef<EnhanceRequest | null>(null);
  useEffect(() => { try { pending.current = JSON.parse(localStorage.getItem(key) || "null"); } catch { pending.current = null; } }, [key]);
  const start = async () => {
    setWorking(true); setNotice("");
    try {
      await beforeRun();
      const latest = currentResult();
      if (!latest || latest.id !== result.id) throw Error("识别结果已切换，请重新选择增强范围。");
      if (scope === "region" && (!box.every(Number.isFinite) || box[0] < 0 || box[1] < 0 || box[2] > result.original.image.width || box[3] > result.original.image.height || box[2] <= box[0] || box[3] <= box[1])) throw Error("区域边界超出当前识别图像，或宽高为空。");
      pending.current ||= { revision: latest.revision, request_id: crypto.randomUUID(), ...(scope === "region" ? { box } : {}) };
      const requestKey = `clear-pro-enhance:${result.id}:${pending.current.revision}`;
      localStorage.setItem(requestKey, JSON.stringify(pending.current));
      await api(`/results/${result.id}/enhance`, "POST", pending.current);
      pending.current = null; localStorage.removeItem(requestKey);
      setNotice("已加入 MinerU Pro 增强识别。旧结果与人工修订已保留；完成后选择新结果，查看固定版本前后对照。");
      await onAccepted();
    } catch (error) { onError(String(error)); }
    finally { setWorking(false); }
  };
  return <details className="result-enhancement"><summary>增强识别 · MinerU Pro</summary>
    <p>对当前页或指定区域重新识别，保存为独立结果。采用前可对照原结果并继续表格校对与导出。</p>
    <div className="comparison-controls"><label>增强范围 <select aria-label="增强识别范围" disabled={working} value={scope} onChange={event => setScope(event.target.value)}>
      <option value="page">当前页 / 图片</option><option value="region">指定区域</option>
    </select></label>
      {scope === "region" && box.map((value, index) => <label key={index}>{["左 X", "上 Y", "右 X", "下 Y"][index]} <input type="number" aria-label={`增强区域${["左", "上", "右", "下"][index]}`} min={0} max={index % 2 ? result.original.image.height : result.original.image.width} value={value} disabled={working}
        onChange={event => setBox(previous => previous.map((old, i) => i === index ? event.target.valueAsNumber : old))} /></label>)}
      <Button disabled={disabled || working} appearance="primary" onClick={() => void start()}>{working ? "正在提交" : pending.current ? "核实原增强请求" : "开始增强识别"}</Button>
    </div>
    {scope === "region" && <p>坐标使用此结果识别输入图像的像素，尺寸 {result.original.image.width} × {result.original.image.height}。局部结果只覆盖所选区域。</p>}
    {notice && <p role="status">{notice}</p>}
  </details>;
}

export function EnhancedResultComparison({ result }: { result: Result }) {
  const [comparison, setComparison] = useState<Comparison | null>(null), [error, setError] = useState("");
  const baseline = result.original.engine_route?.baseline_result_id;
  useEffect(() => {
    let current = true; setComparison(null); setError("");
    if (baseline) void api<Comparison>(`/results/${result.id}/comparison`).then(value => { if (current) setComparison(value); }).catch(value => { if (current) setError(String(value)); });
    return () => { current = false; };
  }, [result.id, baseline]);
  if (!baseline) return null;
  return <section className="comparison-result" aria-label="增强识别前后对照"><h3>增强识别前后对照</h3>
    {error && <p className="inline-warning" role="alert">{error}</p>}
    {comparison?.has_comparison && comparison.baseline && comparison.enhanced ? <>
      <p>增强前固定版本 {comparison.baseline.revision} · {engineNames[comparison.baseline.original.engine] || comparison.baseline.original.engine} → {engineNames[comparison.actual_engine || comparison.enhanced.original.engine] || comparison.actual_engine}</p>
      {comparison.box && <p>本次增强区域：{comparison.box.join(", ")}。对照原页全文时，区域外文字不会出现在局部结果中。</p>}
      <ResultDifference baseline={comparison.baseline} candidate={comparison.enhanced} original />
      <details><summary>查看增强前原始文字与已保存人工修订</summary><h4>原始识别</h4><pre>{comparison.baseline.original.text}</pre><h4>人工修订</h4><pre>{comparison.baseline.edited.text}</pre></details>
    </> : !error && <p>正在读取固定版本对照…</p>}
  </section>;
}
