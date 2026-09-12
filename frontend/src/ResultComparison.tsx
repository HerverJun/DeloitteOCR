import { useMemo, useState } from "react";
import { Button } from "@fluentui/react-components";
import type { Result, Version } from "./types";
import { engineNames } from "./types";
import { documentText, editableResult } from "./documentText";
import {
  alignedTextDiff,
  groupResults,
  tableDifferences,
  type ComparisonTask,
} from "./comparisonOps";

function describeOperations(value: string | undefined): string {
  if (!value) return "未记录";
  try {
    const data = JSON.parse(value);
    if (Array.isArray(data) && !data.length) return "无";
    return JSON.stringify(data);
  } catch {
    return value;
  }
}

function ResultDifference({
  baseline,
  candidate,
  original,
}: {
  baseline: Result;
  candidate: Result;
  original: boolean;
}) {
  const left = original ? baseline.original : baseline.edited;
  const right = original ? candidate.original : candidate.edited;
  const leftText = original ? left.text : documentText(editableResult(baseline)).text;
  const rightText = original ? right.text : documentText(editableResult(candidate)).text;
  const diff = useMemo(
    () => alignedTextDiff(leftText, rightText),
    [leftText, rightText],
  );
  const tableDiff = useMemo(
    () => tableDifferences(left.tables, right.tables),
    [left.tables, right.tables],
  );
  const [context, setContext] = useState(false);
  const [textLimit, setTextLimit] = useState(200);
  const [tableLimit, setTableLimit] = useState(100);
  const added = diff.lines.filter((line) => line.kind === "added").length;
  const removed = diff.lines.filter((line) => line.kind === "removed").length;
  const lines = context
    ? diff.lines
    : diff.lines.filter((line) => line.kind !== "context");
  return (
    <div className="difference-note">
      <p>
        与基准比较：
        {!added && !removed
          ? "文字一致"
          : `新增 ${added} 行，删除 ${removed} 行`}
        ；表格差异 {tableDiff.length} 项。
      </p>
      {diff.coarse && (
        <p className="inline-warning">
          部分长段落缺少可靠对齐点，已按整段替换展示。
        </p>
      )}
      <details>
        <summary>查看对齐文本差异</summary>
        <label>
          <input
            type="checkbox"
            checked={context}
            onChange={(event) => setContext(event.target.checked)}
          />{" "}
          显示相同的上下文
        </label>
        {!!lines.length && (
          <div className="comparison-diff-scroll">
            <table className="aligned-diff">
              <thead>
                <tr>
                  <th scope="col">基准行</th>
                  <th scope="col">当前行</th>
                  <th scope="col">内容</th>
                </tr>
              </thead>
              <tbody>
                {lines.slice(0, textLimit).map((line, i) => (
                  <tr key={i} className={`diff-${line.kind}`}>
                    <td>{line.before ?? ""}</td>
                    <td>{line.after ?? ""}</td>
                    <td>
                      <span
                        aria-label={
                          line.kind === "added"
                            ? "新增"
                            : line.kind === "removed"
                              ? "删除"
                              : "相同"
                        }
                      >
                        {line.kind === "added"
                          ? "+ "
                          : line.kind === "removed"
                            ? "− "
                            : "  "}
                      </span>
                      {line.text || " "}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {lines.length > textLimit && (
          <Button
            size="small"
            onClick={() => setTextLimit((value) => value + 500)}
          >
            再显示 500 行（剩余 {lines.length - textLimit}）
          </Button>
        )}
      </details>
      <details className="comparison-table-diff">
        <summary>查看单元格、行列与合并差异</summary>
        <p>按表格顺序和单元格位置比较；新增行列可能同时产生单元格位置差异。</p>
        {!tableDiff.length ? (
          <p>表格结构与单元格文字一致。</p>
        ) : (
          <div className="comparison-diff-scroll">
            <table className="aligned-diff">
              <thead>
                <tr>
                  <th scope="col">位置</th>
                  <th scope="col">基准</th>
                  <th scope="col">当前</th>
                </tr>
              </thead>
              <tbody>
                {tableDiff.slice(0, tableLimit).map((difference, i) => (
                  <tr key={i}>
                    <th scope="row">
                      表 {difference.table} · {difference.location}
                    </th>
                    <td>{difference.before || "（空）"}</td>
                    <td>{difference.after || "（空）"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {tableDiff.length > tableLimit && (
          <Button
            size="small"
            onClick={() => setTableLimit((value) => value + 200)}
          >
            再显示 200 项（剩余 {tableDiff.length - tableLimit}）
          </Button>
        )}
      </details>
    </div>
  );
}

export function ResultComparison({
  results,
  tasks,
  versions,
  selectedResultId,
  onAdopt,
  adoptingDisabled = false,
  reviewStatus = "pending",
  reviewedResultId,
  reviewedRevision,
}: {
  results: Result[];
  tasks: ComparisonTask[];
  versions: Version[];
  selectedResultId?: string | null;
  onAdopt: (id: string) => void;
  adoptingDisabled?: boolean;
  reviewStatus?: "pending" | "confirmed" | "question";
  reviewedResultId?: string | null;
  reviewedRevision?: number | null;
}) {
  const [groupKey, setGroupKey] = useState("");
  const [baselineId, setBaselineId] = useState("");
  const [original, setOriginal] = useState(true);
  const groups = useMemo(() => groupResults(results, tasks), [results, tasks]);
  const group = groups.find((value) => value.key === groupKey) ?? groups[0];
  const candidates = group?.results ?? [];
  const baseline =
    candidates.find((value) => value.id === baselineId) ?? candidates[0];
  const versionLabel = (id: string) => {
    const index = versions.findIndex((value) => value.id === id);
    return index < 0 ? `版本 ${id.slice(0, 8)}` : `图像版本 ${index + 1}`;
  };
  if (!results.length)
    return (
      <div className="comparison-panel">
        <p>当前没有可比较的识别结果。</p>
      </div>
    );
  return (
    <div className="comparison-panel">
      <p>
        按相同图像版本和识别批次比较。选择基准后查看模型原文或保存的校对内容，再明确采用结果。
      </p>
      <div className="comparison-controls">
        <label>
          比较批次{" "}
          <select
            aria-label="比较批次"
            value={group?.key ?? ""}
            onChange={(event) => {
              setGroupKey(event.target.value);
              setBaselineId("");
            }}
          >
            {groups.map((value) => (
              <option key={value.key} value={value.key}>
                {versionLabel(value.versionId)} ·{" "}
                {value.created
                  ? new Date(value.created).toLocaleString()
                  : "时间未知"}{" "}
                · {value.batch ? `批次 ${value.batch.slice(-8)}` : "批次未知"} ·{" "}
                {value.results.length} 项
              </option>
            ))}
          </select>
        </label>
        <label>
          比较基准{" "}
          <select
            aria-label="比较基准"
            value={baseline?.id ?? ""}
            onChange={(event) => setBaselineId(event.target.value)}
          >
            {candidates.map((result) => (
              <option key={result.id} value={result.id}>
                {engineNames[result.original.engine] ?? result.original.engine}{" "}
                · {result.id.slice(0, 8)}
              </option>
            ))}
          </select>
        </label>
        <label>
          内容视图{" "}
          <select
            aria-label="比较内容视图"
            value={original ? "original" : "edited"}
            onChange={(event) => setOriginal(event.target.value === "original")}
          >
            <option value="original">模型原始识别</option>
            <option value="edited">保存的人工校对</option>
          </select>
        </label>
      </div>
      {candidates.length < 2 && (
        <p className="inline-warning">
          这一图像版本和批次只有一个结果。可切换批次，或用「四引擎顺序对比」创建同条件结果。
        </p>
      )}
      {candidates.map((result) => {
        const task = tasks.find((value) => value.id === result.task_id);
        const versionId =
          result.original.project_image_version ?? task?.version_id;
        const version = versions.find((value) => value.id === versionId);
        const reviewMatches =
          result.id === reviewedResultId &&
          result.revision === reviewedRevision;
        const review =
          reviewMatches && reviewStatus === "confirmed"
            ? "已确认"
            : reviewMatches && reviewStatus === "question"
              ? "有疑问"
              : "待校对";
        const content = original ? result.original : result.edited;
        const contentText = original ? content.text : documentText(editableResult(result)).text;
        return (
          <article className="comparison-result" key={result.id}>
            <header>
              <strong>
                {engineNames[result.original.engine] ?? result.original.engine}
                {result.id === baseline?.id ? " · 基准" : ""}
              </strong>
              <span>{typeof result.original.elapsed_seconds === "number" ? `${result.original.elapsed_seconds.toFixed(2)} 秒` : "耗时未记录"}</span>
              <Button
                size="small"
                appearance={
                  result.id === selectedResultId ? "primary" : "secondary"
                }
                disabled={adoptingDisabled || result.id === selectedResultId}
                onClick={() => onAdopt(result.id)}
              >
                {result.id === selectedResultId ? "已采用" : "采用结果"}
              </Button>
            </header>
            <dl className="comparison-metadata">
              <div>
                <dt>识别输入</dt>
                <dd>
                  {versionId ? versionLabel(versionId) : "版本未知"}
                  {version ? ` · ${version.width} × ${version.height}` : ""}
                </dd>
              </div>
              <div>
                <dt>输入处理</dt>
                <dd>{describeOperations(version?.operations)}</dd>
              </div>
              <div>
                <dt>批次预处理</dt>
                <dd>{describeOperations(task?.preprocess)}</dd>
              </div>
              <div>
                <dt>模型包</dt>
                <dd>{task?.engine_package ?? "未记录"}</dd>
              </div>
              <div>
                <dt>识别时间</dt>
                <dd>
                  {task?.created
                    ? new Date(task.created).toLocaleString()
                    : "未知"}
                </dd>
              </div>
              <div>
                <dt>人工校对</dt>
                <dd>
                  {review} · revision {result.revision}
                  {result.revision > 0 ? " · 有保存历史" : " · 尚无保存历史"}
                </dd>
              </div>
              <div>
                <dt>结果 ID</dt>
                <dd>{result.id}</dd>
              </div>
            </dl>
            <details>
              <summary>
                {original ? "查看模型原文" : "查看保存的校对文字"} ·{" "}
                {contentText.length} 字符 / {content.tables.length} 个表格
              </summary>
              <pre>{contentText}</pre>
            </details>
            {baseline && result.id !== baseline.id && (
              <ResultDifference
                key={`${baseline.id}:${result.id}:${original}`}
                baseline={baseline}
                candidate={result}
                original={original}
              />
            )}
          </article>
        );
      })}
    </div>
  );
}
