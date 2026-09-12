import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Result, Edit } from "./types";

export function useEditor(onError: (message: string) => void) {
  const [result, setResult] = useState<Result | null>(null);
  const [edit, setEdit] = useState<Edit | null>(null);
  const [saveState, setSaveState] = useState("已保存");
  const [loadState, setLoadState] = useState<
    "idle" | "loading" | "ready" | "failed"
  >("idle");
  const [loadError, setLoadError] = useState("");
  const requested = useRef<string | null>(null);
  const current = useRef<Result | null>(null);
  const pending = useRef<Edit | null>(null);
  const flight = useRef<Promise<void> | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const loadGeneration = useRef(0);
  // Saves, history and reloads share one queue, including during navigation.
  const enqueue = useCallback((run: () => Promise<void>) => {
    const operation = (flight.current || Promise.resolve())
      .catch(() => {})
      .then(run);
    flight.current = operation;
    const clear = () => {
      if (flight.current === operation) flight.current = null;
    };
    void operation.then(clear, clear);
    return operation;
  }, []);
  const savePending = useCallback(async () => {
    while (pending.current && current.current) {
      const target = current.current;
      const generation = loadGeneration.current;
      const value = pending.current;
      pending.current = null;
      setSaveState("保存中");
      try {
        const saved = await api<Result>("/results/" + target.id, "PUT", {
          edited: value,
          revision: target.revision,
        });
        current.current = saved;
        if (generation === loadGeneration.current) setResult(saved);
        if (!pending.current && generation === loadGeneration.current) {
          setEdit(saved.edited);
          setSaveState("已保存");
        }
      } catch (error) {
        pending.current = pending.current || value;
        setSaveState("保存失败");
        onError(String(error));
        throw error;
      }
    }
  }, [onError]);
  const flush = useCallback(() => enqueue(savePending), [enqueue, savePending]);
  const load = useCallback(
    async (id: string | null) => {
      const generation = ++loadGeneration.current;
      await flush();
      if (generation !== loadGeneration.current) return;
      requested.current = id;
      setLoadError("");
      if (id === current.current?.id) {
        setResult(current.current);
        setEdit(current.current.edited);
        setSaveState("已保存");
        setLoadState("ready");
        return;
      }
      setResult(null);
      setEdit(null);
      current.current = null;
      if (!id) {
        current.current = null;
        setResult(null);
        setEdit(null);
        setLoadState("idle");
        return;
      }
      setLoadState("loading");
      try {
        const value = await api<Result>("/results/" + id);
        if (generation !== loadGeneration.current) return;
        current.current = value;
        setResult(value);
        setEdit(value.edited);
        setSaveState("已保存");
        setLoadState("ready");
      } catch (error) {
        if (generation === loadGeneration.current) {
          setLoadState("failed");
          setLoadError(String(error));
        }
        throw error;
      }
    },
    [flush],
  );
  const change = useCallback(
    (value: Edit) => {
      setEdit(value);
      pending.current = value;
      setSaveState("待保存");
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        void flush().catch(() => {});
      }, 650);
    },
    [flush],
  );
  const history = useCallback(
    async (direction: number) => {
      const id = current.current?.id;
      const generation = loadGeneration.current;
      await enqueue(async () => {
        if (
          !id ||
          current.current?.id !== id ||
          generation !== loadGeneration.current
        )
          return;
        await savePending();
        if (current.current?.id !== id || generation !== loadGeneration.current)
          return;
        const value = await api<Result>("/results/" + id + "/history", "POST", {
          direction,
          revision: current.current.revision,
        });
        if (current.current?.id !== id) return;
        current.current = value;
        if (generation !== loadGeneration.current) return;
        setResult(value);
        setEdit(value.edited);
        setSaveState("已保存");
      });
    },
    [enqueue, savePending],
  );
  // Reload is an explicit discard action when a local draft exists. Commit the
  // replacement only after a successful read, retaining dirty state on failure.
  const reload = useCallback(async () => {
    const id = requested.current || current.current?.id;
    const generation = loadGeneration.current;
    if (timer.current) clearTimeout(timer.current);
    await enqueue(async () => {
      if (!id || generation !== loadGeneration.current) return;
      const draft = pending.current;
      setLoadState("loading");
      try {
        const value = await api<Result>("/results/" + id);
        if (generation !== loadGeneration.current) return;
        if (pending.current !== draft)
          throw Error("读取期间草稿已变化，已保留本地修改，请重试。");
        pending.current = null;
        current.current = value;
        setResult(value);
        setEdit(value.edited);
        setSaveState("已保存");
        setLoadState("ready");
        setLoadError("");
      } catch (error) {
        if (generation === loadGeneration.current) {
          setLoadState("failed");
          setLoadError(String(error));
        }
        throw error;
      }
    });
  }, [enqueue]);
  const downloadDraft = useCallback(() => {
    const value = pending.current || current.current?.edited;
    if (!value) return;
    const blob = new Blob(
      [
        JSON.stringify(
          {
            result_id: current.current?.id,
            revision: current.current?.revision,
            edited: value,
          },
          null,
          2,
        ),
      ],
      { type: "application/json" },
    );
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `OCR-draft-${current.current?.id || "local"}.json`;
    anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }, []);
  useEffect(() => {
    const before = (event: BeforeUnloadEvent) => {
      if (pending.current || flight.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", before);
    return () => {
      window.removeEventListener("beforeunload", before);
      if (timer.current) clearTimeout(timer.current);
    };
  }, []);
  const getCurrent = useCallback(() => current.current, []);
  return {
    result,
    edit,
    change,
    load,
    flush,
    history,
    reload,
    saveState,
    loadState,
    loadError,
    downloadDraft,
    getCurrent,
  };
}
