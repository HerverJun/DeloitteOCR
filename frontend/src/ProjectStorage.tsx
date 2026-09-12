import { useEffect, useState } from "react";
import {
  Button,
  Dialog,
  DialogSurface,
  DialogBody,
  DialogTitle,
  DialogContent,
  DialogActions,
  Spinner,
} from "@fluentui/react-components";
import { api } from "./api";

export function ProjectStorage({
  id,
  name,
  onDelete,
  onError,
}: {
  id: string;
  name: string;
  onDelete: (confirmation: string) => Promise<void>;
  onError: (message: string) => void;
}) {
  const [open, setOpen] = useState(false),
    [confirmation, setConfirmation] = useState(""),
    [busy, setBusy] = useState(false);
  const [usage, setUsage] = useState<{
    bytes: number;
    files: number;
    scope: string;
    file_bytes?: number;
    history_bytes?: number;
    history_entries?: number;
    database_payload_bytes?: number;
    workspace_database_bytes?: number;
    free_bytes?: number;
    warning?: string | null;
  } | null>(null);
  const [orphans, setOrphans] = useState<{
    files: { path: string; bytes: number; reason: string }[];
    count: number;
    policy: string;
  } | null>(null);
  const [chosen, setChosen] = useState<string[]>([]);
  const [quarantineReceipt, setQuarantineReceipt] = useState("");
  const [localError, setLocalError] = useState("");
  const reportError = (message: string) => { setLocalError(message); onError(message); };
  useEffect(() => {
    if (!open || !id) return;
    let active = true;
    setUsage(null);
    setLocalError("");
    setConfirmation("");
    api("/projects/" + id + "/storage")
      .then((value) => {
        if (active) setUsage(value);
      })
      .catch((error) => reportError(String(error)));
    return () => {
      active = false;
    };
  }, [open, id]);
  return (
    <>
      <Button
        aria-label="项目占用与清理"
        size="small"
        appearance="subtle"
        disabled={!id}
        onClick={() => setOpen(true)}
      >
        项目占用与清理
      </Button>
      <Dialog
        open={open}
        onOpenChange={(_, data) => {
          if (!busy) setOpen(data.open);
        }}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>项目占用与清理</DialogTitle>
            <DialogContent>
              {localError && <div className="inline-warning" role="alert">{localError}</div>}
              {usage ? (
                <>
                  <p>
                    <strong>{name}</strong> ·{" "}
                    {(usage.bytes / 1024 / 1024).toFixed(1)} MB · {usage.files}{" "}
                    个文件
                  </p>
                  <p>{usage.scope}</p>
                  {usage.history_bytes !== undefined && (
                    <p>
                      文件 {((usage.file_bytes || 0) / 1048576).toFixed(1)}{" "}
                      MB；项目数据库内容{" "}
                      {((usage.database_payload_bytes || 0) / 1048576).toFixed(
                        1,
                      )}{" "}
                      MB（含 {usage.history_entries} 条完整撤销历史，压缩后{" "}
                      {(usage.history_bytes / 1048576).toFixed(2)}{" "}
                      MB）。共享数据库{" "}
                      {(
                        (usage.workspace_database_bytes || 0) / 1048576
                      ).toFixed(1)}{" "}
                      MB；磁盘可用{" "}
                      {((usage.free_bytes || 0) / 1073741824).toFixed(1)} GB。
                    </p>
                  )}
                  {usage.warning && <p role="alert">{usage.warning}</p>}
                  <details>
                    <summary>审查工作区未登记文件</summary>
                    <p>
                      扫描整个工作区，只列出未被数据库引用的图片文件。选择后移入隔离目录，并保留原路径清单以便恢复。
                    </p>
                    <Button
                      disabled={busy}
                      onClick={async () => {
                        setBusy(true);
                        try {
                          setOrphans(await api("/maintenance/orphans"));
                          setChosen([]);
                        } catch (error) {
                          reportError(String(error));
                        } finally {
                          setBusy(false);
                        }
                      }}
                    >
                      扫描未登记文件
                    </Button>
                    {orphans && (
                      <>
                        <p>
                          {orphans.count} 个路径 · {orphans.policy}
                        </p>
                        <div className="orphan-list">
                          {orphans.files.map((file) => (
                            <label key={file.path}>
                              <input
                                type="checkbox"
                                checked={chosen.includes(file.path)}
                                onChange={(e) =>
                                  setChosen((old) =>
                                    e.target.checked
                                      ? [...old, file.path]
                                      : old.filter((p) => p !== file.path),
                                  )
                                }
                              />
                              {file.path} · {file.reason} ·{" "}
                              {(file.bytes / 1048576).toFixed(2)} MB
                            </label>
                          ))}
                        </div>
                        <Button
                          disabled={busy || !chosen.length}
                          onClick={async () => {
                            setBusy(true);
                            try {
                              const result = await api(
                                "/maintenance/orphans/quarantine",
                                "POST",
                                { paths: chosen },
                              );
                              setQuarantineReceipt(
                                `已隔离 ${result.quarantined} 个路径。恢复清单：${result.ledger}`,
                              );
                              setOrphans(await api("/maintenance/orphans"));
                              setChosen([]);
                            } catch (error) {
                              reportError(String(error));
                            } finally {
                              setBusy(false);
                            }
                          }}
                        >
                          隔离所选文件（保留恢复清单）
                        </Button>
                      </>
                    )}
                    {quarantineReceipt && (
                      <p role="status">{quarantineReceipt}</p>
                    )}
                  </details>
                </>
              ) : (
                <Spinner label="计算项目占用" />
              )}
              <p>
                清理会永久删除此项目的原图、处理版本、识别结果及人工校对。请先导出或备份需要保留的数据；运行中的项目须先取消任务。
              </p>
              <label className="dialog-field">
                输入完整项目名称以确认
                <input
                  aria-label="确认清理项目名称"
                  value={confirmation}
                  onChange={(e) => setConfirmation(e.target.value)}
                />
              </label>
            </DialogContent>
            <DialogActions>
              <Button disabled={busy} onClick={() => setOpen(false)}>
                取消
              </Button>
              <Button
                aria-label="清理并删除项目"
                appearance="primary"
                disabled={busy || !usage || confirmation !== name}
                onClick={async () => {
                  setBusy(true);
                  try {
                    await onDelete(confirmation);
                    setOpen(false);
                  } catch (error) {
                    reportError(String(error));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                清理并删除项目
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </>
  );
}
