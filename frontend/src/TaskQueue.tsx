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
}: {
  project: ProjectState;
  onAction: (name: string, task?: Task) => void;
  onView: (task: Task) => void;
  onClose: () => void;
}) {
  return (
    <section className="task-drawer" aria-label="任务队列">
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
            onClick={() => onAction("resume")}
          >
            继续
          </Button>
          <Button
            size="small"
            icon={<RefreshCw size={14} />}
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
      <div className="task-rows">
        {project.tasks
          .slice()
          .reverse()
          .map((t) => (
            <div className="task-row" key={t.id}>
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
                {statuses[t.status]}
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
              <span>{engineNames[t.engine]}</span>
              <div>
                {t.result_id || t.result_version_id ? (
                  <Button size="small" onClick={() => onView(t)}>
                    {t.result_id ? "查看" : "查看图片"}
                  </Button>
                ) : (
                  <Button
                    size="small"
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
        {!project.tasks.length && (
          <p className="queue-empty">尚无任务。选择图片后开始识别。</p>
        )}
      </div>
    </section>
  );
}
