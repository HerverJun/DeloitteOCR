import { AlertCircle, CheckCircle2 } from "lucide-react";
import { engineNames } from "./types";
import { formatCapacity, gpuDescription, parseDoctorReport, type DiagnosticsResponse } from "./diagnostics";

export function DiagnosticsPanel({ diagnostics }: { diagnostics: DiagnosticsResponse }) {
  const report = parseDoctorReport(diagnostics.details);
  const complete = diagnostics.passed && report?.gpu_exit === 0 && report.missing?.length === 0 &&
    Object.values(report.runtimes ?? {}).every(found => found);
  const missing = report?.missing ?? [];
  const runtimes = Object.entries(report?.runtimes ?? {});
  const gpuOk = report?.gpu_exit === 0;

  return <section className="environment-report" aria-label="环境检查结果">
    <div className={`environment-status ${complete ? "is-success" : "is-warning"}`} role="status">
      {complete ? <CheckCircle2 size={22} aria-hidden="true" /> : <AlertCircle size={22} aria-hidden="true" />}
      <div><strong>{complete ? "环境检查通过" : diagnostics.passed ? "检查完成，结果需复核" : "发现环境问题"}</strong>
        <p>{complete ? "驱动查询正常，所需运行时与模型清单文件均已找到。" :
          diagnostics.passed ? "检查工具已返回成功，但结果不完整或与状态不一致；请查看技术详情。" :
          "请查看下方未通过的项目；如果检查未能完成，请展开技术详情。"}</p></div>
    </div>
    {report ? <div className="environment-checks">
      <div className="environment-check"><span>显卡与驱动</span><strong className={gpuOk ? "check-ok" : "check-failed"}>{gpuOk ? "已检测" : "查询失败"}</strong><small>{gpuOk ? gpuDescription(report.gpu) : "无法通过 nvidia-smi 查询显卡；请确认驱动已安装。"}</small></div>
      <div className="environment-check"><span>识别运行时</span><strong className={runtimes.length && runtimes.every(([, found]) => found) ? "check-ok" : "check-failed"}>{runtimes.filter(([, found]) => found).length} / {runtimes.length} 已找到</strong>
        <small>{runtimes.map(([name, found]) => `${engineNames[name] || name}：${found ? "已找到" : "缺失"}`).join(" · ") || "未获得运行时列表"}</small></div>
      <div className="environment-check"><span>离线文件</span><strong className={missing.length ? "check-failed" : "check-ok"}>{missing.length ? `${missing.length} 项缺失` : "无缺失项"}</strong>
        <small>{missing.length ? missing.map(path => <span className="environment-missing" key={path}>{path}</span>) : "检查范围内未发现缺失文件。"}</small></div>
      <div className="environment-check"><span>数据磁盘可用空间</span><strong>{formatCapacity(diagnostics.free_bytes)}</strong><small>仅显示剩余空间，不代表已完成写入测试。</small></div>
    </div> : <p className="environment-unavailable">没有可解析的检查数据，无法逐项确认运行环境。</p>}
    <details className="environment-technical"><summary>查看技术详情</summary>
      {report && <dl><div><dt>系统</dt><dd>{report.platform || "未记录"}</dd></div><div><dt>Python</dt><dd>{report.python?.split("\n")[0] || "未记录"}</dd></div><div><dt>程序目录</dt><dd>{report.bundle || "未记录"}</dd></div><div><dt>程序磁盘可用</dt><dd>{formatCapacity(report.disk_free_bytes)}</dd></div></dl>}
      {diagnostics.error && <p role="alert">{diagnostics.error}</p>}
      {diagnostics.details && <pre className="diagnostics">{diagnostics.details}</pre>}
    </details>
  </section>;
}
