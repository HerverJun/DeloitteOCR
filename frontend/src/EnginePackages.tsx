import { useState, useRef, useEffect } from "react";
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
import { api, request } from "./api";
type Package = { id: string; engine: string; name: string; version: string };
type Inventory = {
  engines: Record<string, { name: string; package_id: string }>;
  installed: Package[];
  active: Record<string, string>;
  staged: (Partial<Staged> & { staging_id: string; ready: boolean })[];
};
type Staged = {
  staging_id: string;
  sha256: string;
  id: string;
  engine: string;
  name: string;
  version: string;
  bytes: number;
  files: number;
};

export function EnginePackages({
  onChange,
  onError,
}: {
  onChange: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [open, setOpen] = useState(false),
    [busy, setBusy] = useState(false),
    [inventory, setInventory] = useState<Inventory | null>(null),
    [staged, setStaged] = useState<Staged | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const surface = useRef<HTMLDivElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (open && !busy) closeButton.current?.focus();
  }, [open, busy]);
  const refresh = async () => setInventory(await api("/engine-packages"));
  const run = async (action: () => Promise<void>) => {
    // Disabling the focused native select blurs to body. Keep focus inside the
    // modal so Tabster does not hide the still-open dialog from screen readers.
    surface.current?.focus();
    setBusy(true);
    try {
      await action();
    } catch (error) {
      onError(String(error));
    } finally {
      setBusy(false);
    }
  };
  const close = async () => {
    if (staged)
      await api("/engine-packages/staging/" + staged.staging_id, "DELETE");
    setStaged(null);
    setOpen(false);
  };
  return (
    <>
      <Button
        appearance="subtle"
        onClick={() =>
          void run(async () => {
            setOpen(true);
            await refresh();
          })
        }
      >
        引擎管理
      </Button>
      <input
        type="file"
        ref={file}
        accept=".zip"
        hidden
        aria-label="选择离线引擎包"
        onChange={(event) => {
          const value = event.target.files?.[0];
          event.target.value = "";
          if (value)
            void run(async () => {
              setStaged(
                await (
                  await request("/engine-packages/stage", {
                    method: "POST",
                    body: value,
                    headers: { "Content-Type": "application/zip" },
                  })
                ).json(),
              );
            });
        }}
      />
      <Dialog
        open={open}
        onOpenChange={(_, data) => {
          if (!data.open && !busy) void run(close);
        }}
      >
        <DialogSurface ref={surface}>
          <DialogBody>
            <DialogTitle>离线引擎管理</DialogTitle>
            <DialogContent>
              <p>
                导入完整 ZIP
                中的模型、运行时和适配器。校验只证明文件完整；启用将执行包内代码，请仅使用可信来源的引擎包。
              </p>
              {busy && <Spinner label="正在校验或切换完整引擎包，请稍候" />}
              {staged ? (
                <div>
                  <h3>
                    {staged.name} · {staged.version}
                  </h3>
                  <p>
                    {staged.files} 个文件 ·{" "}
                    {(staged.bytes / 1024 ** 3).toFixed(2)} GB
                  </p>
                  <p style={{ overflowWrap: "anywhere" }}>
                    SHA-256：{staged.sha256}
                  </p>
                  <p>
                    启用后，新任务使用此版本；已有任务和历史结果仍关联原版本。
                  </p>
                  <Button
                    aria-label="启用此引擎包"
                    disabled={busy}
                    appearance="primary"
                    onClick={() =>
                      void run(async () => {
                        await api("/engine-packages/activate", "POST", staged);
                        setStaged(null);
                        await refresh();
                        await onChange();
                      })
                    }
                  >
                    启用此引擎包
                  </Button>
                  <Button
                    disabled={busy}
                    onClick={() =>
                      void run(async () => {
                        await api(
                          "/engine-packages/staging/" + staged.staging_id,
                          "DELETE",
                        );
                        setStaged(null);
                      })
                    }
                  >
                    放弃暂存
                  </Button>
                </div>
              ) : (
                <div>
                  <Button disabled={busy} onClick={() => file.current?.click()}>
                    导入完整离线引擎包
                  </Button>
                  {inventory?.staged.map((item) => (
                    <div key={item.staging_id} className="dialog-field">
                      <span>
                        {item.ready
                          ? `${item.name} · ${item.version}`
                          : "未完成的暂存"}
                      </span>
                      {item.ready && (
                        <Button
                          disabled={busy}
                          onClick={() => setStaged(item as Staged)}
                        >
                          继续此暂存包
                        </Button>
                      )}
                      <Button
                        disabled={busy}
                        onClick={() =>
                          void run(async () => {
                            await api(
                              "/engine-packages/staging/" + item.staging_id,
                              "DELETE",
                            );
                            await refresh();
                          })
                        }
                      >
                        删除此暂存
                      </Button>
                    </div>
                  ))}
                </div>
              )}
              {inventory &&
                Object.entries(inventory.engines).map(([engine, spec]) => (
                  <div key={engine} className="dialog-field">
                    <strong>{spec.name}</strong>
                    <label>
                      启用版本
                      <select
                        aria-label={spec.name + " 启用版本"}
                        disabled={busy}
                        value={spec.package_id}
                        onChange={(event) =>
                          void run(async () => {
                            await api("/engine-packages/switch", "POST", {
                              engine,
                              package_id: event.target.value,
                            });
                            await refresh();
                            await onChange();
                          })
                        }
                      >
                        {["ppocr", "paddlevl", "glm", "hunyuan"].includes(
                          engine,
                        ) && <option value="builtin">随应用内置版本</option>}
                        {inventory.installed
                          .filter((p) => p.engine === engine)
                          .map((p) => (
                            <option key={p.id} value={p.id}>
                              {p.version} · {p.id}
                            </option>
                          ))}
                      </select>
                    </label>
                  </div>
                ))}
            </DialogContent>
            <DialogActions>
              <Button
                ref={closeButton}
                aria-label="关闭"
                disabled={busy}
                onClick={() => void run(close)}
              >
                关闭
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </>
  );
}
