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
  } | null>(null);
  useEffect(() => {
    if (!open || !id) return;
    let active = true;
    setUsage(null);
    setConfirmation("");
    api("/projects/" + id + "/storage")
      .then((value) => {
        if (active) setUsage(value);
      })
      .catch((error) => onError(String(error)));
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
              {usage ? (
                <>
                  <p>
                    <strong>{name}</strong> ·{" "}
                    {(usage.bytes / 1024 / 1024).toFixed(1)} MB · {usage.files}{" "}
                    个文件
                  </p>
                  <p>{usage.scope}</p>
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
                    onError(String(error));
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
