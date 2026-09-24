import { useLayoutEffect, useRef, useState } from "react";
import { Button, Spinner } from "@fluentui/react-components";
import {
  Pause,
  Play,
  RefreshCw,
  X,
  CheckCircle2,
  AlertCircle,
} from "lucide-react";
import type { ProjectState, Task } from "./types";
import { statuses, engineNames } from "./types";
export function TaskQueue({
  project,
  onAction,
  onView,
  onClose,
  focusRequest,
  recognitionDisabled = false,
}: {
  project: ProjectState;
  onAction: (name: string, task?: Task) => void;
  onView: (task: Task) => void;
  onClose: () => void;
  focusRequest?: { taskId: string } | null;
  recognitionDisabled?: boolean;
}) {
  const [limit, setLimit] = useState(60);
  const drawer = useRef<HTMLElement>(null);
  const rows = useRef<HTMLDivElement>(null);
  const focusedTask = useRef<HTMLDivElement>(null);
  const orderedTasks = project.tasks.slice().reverse();
  const visibleLimit = Math.max(limit, orderedTasks.findIndex(t => t.id === focusRequest?.taskId) + 1);
  useLayoutEffect(() => {
    if (!focusRequest) return;
    const row = focusedTask.current;
    if (row && rows.current) {
      const bounds = row.getBoundingClientRect();
      const viewport = rows.current.getBoundingClientRect();
      // Scroll only the task list; outer workspace containers must stay put.
      rows.current.scrollTop += bounds.top - viewport.top - Math.max(0, (viewport.height - bounds.height) / 2);
    }
    (row || drawer.current)?.focus({ preventScroll: true });
  }, [focusRequest]);
  return (
    <section id="task-queue" ref={drawer} className="task-drawer" aria-label="任务队列" tabIndex={-1}>
      <header>
        <h3>
          任务队列 <span>{project.tasks.length || 0}</span>
          <small className="queue-counts">
            已完成{" "}
            {project.tasks.filter((t) => t.status === "succeeded").length} ·
            失败 {project.tasks.filter((t) => t.status === "failed").length}
          </small>
        </h3>
        <div>
          <Button
            size="small"
            icon={<Pause size={14} />}
            onClick={() => onAction("pause")}
          >
            暂停等待项
          </Button>
          <Button
            size="small"
            icon={<Play size={14} />}
            disabled={recognitionDisabled && !project.tasks.some(t => t.kind === "fusion" || t.review_backend === "external")}
            onClick={() => onAction("resume")}
          >
            继续
          </Button>
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
            disabled={recognitionDisabled && !project.tasks.some(t => t.kind === "fusion" || t.review_backend === "external")}
            onClick={() => onAction("retry")}
          >
            重试失败项
          </Button>
          <Button
            size="small"
            appearance="subtle"
            aria-label="收起任务队列"
            icon={<X size={16} />}
            onClick={onClose}
          />
        </div>
      </header>
      {project.external_queue?.healthy === false && <div className="inline-warning" role="status">
        外部审校队列当前不可用。
        <Button size="small" onClick={() => onAction("recover_external")}>恢复外部审校队列</Button>
      </div>}
      <div className="task-rows" ref={rows}>
        {orderedTasks
          .slice(0, visibleLimit)
          .map((t) => (
            <div className="task-row" key={t.id} data-task-id={t.id} tabIndex={-1}
              ref={t.id === focusRequest?.taskId ? focusedTask : undefined}>
              <span className={"task-status " + t.status}>
                {t.status === "succeeded" ? (
                  <CheckCircle2 size={15} />
                ) : t.status === "failed" ? (
                  <AlertCircle size={15} />
                ) : t.status === "running" ? (
                  <Spinner size="tiny" />
                ) : (
                  <span className="task-dot" />
                )}
                {(t.kind === "multimodal" || t.engine === "reviewer") && ["queued", "running"].includes(t.status)
                  ? t.status === "queued" ? "等待审校" : "审校中"
                  : statuses[t.status]}
              </span>
              <span className="task-filename" title={t.error || undefined}>
                {project.images.find((p) => p.id === t.image_id)?.name}
                <small>
                  {t.error ? "处理失败，可展开原因后重试" : t.phase}
                </small>
                {t.error && (
                  <details className="task-error">
                    <summary>错误详情</summary>
                    <p>{t.error}</p>
                  </details>
                )}
              </span>
              <span>{t.review_backend === "external" ? "外部 API 审校" : engineNames[t.engine]}</span>
              <div>
                {t.kind === "multimodal" || t.engine === "reviewer" ? <>
                  <Button size="small" onClick={() => onView(t)}>查看审校</Button>
                  {t.status !== "succeeded" && <Button size="small" disabled={recognitionDisabled && t.review_backend !== "external" && ["failed", "cancelled", "paused", "interrupted"].includes(t.status)} onClick={() => onAction(
                    ["failed", "cancelled"].includes(t.status) ? "retry" : ["paused", "interrupted"].includes(t.status) ? "resume" : "cancel", t)}>
                    {["failed", "cancelled"].includes(t.status) ? "重试" : ["paused", "interrupted"].includes(t.status) ? "继续" : "取消"}
                  </Button>}
                </> : t.result_id || t.result_version_id ? (
                  <Button size="small" onClick={() => onView(t)}>
                    {t.result_id ? "查看" : "查看图片"}
                  </Button>
                ) : (
                  <Button
                    size="small"
                    disabled={
                      recognitionDisabled && t.kind !== "fusion" &&
                      ["failed", "cancelled", "paused", "interrupted"].includes(
                        t.status,
                      )
                    }
                    onClick={() =>
                      onAction(
                        ["failed", "cancelled"].includes(t.status)
                          ? "retry"
                          : ["paused", "interrupted"].includes(t.status)
                            ? "resume"
                            : "cancel",
                        t,
                      )
                    }
                  >
                    {["failed", "cancelled"].includes(t.status)
                      ? "重试"
                      : ["paused", "interrupted"].includes(t.status)
                        ? "继续"
                        : "取消"}
                  </Button>
                )}
              </div>
            </div>
          ))}
        {project.tasks.length > visibleLimit && (
          <button className="load-more" onClick={() => setLimit(visibleLimit + 60)}>
            继续显示历史任务 ({visibleLimit}/{project.tasks.length})
          </button>
        )}
        {!project.tasks.length && (
          <p className="queue-empty">尚无任务。选择图片后开始识别。</p>
        )}
      </div>
    </section>
  );
}
