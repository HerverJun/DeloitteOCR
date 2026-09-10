import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Result, Edit } from "./types";

export function useEditor(onError: (message: string) => void) {
  const [result, setResult] = useState<Result | null>(null);
  const [edit, setEdit] = useState<Edit | null>(null);
  const [saveState, setSaveState] = useState("已保存");
  const current = useRef<Result | null>(null);
  const pending = useRef<Edit | null>(null);
  const flight = useRef<Promise<void> | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const loadGeneration = useRef(0);
  const flush = useCallback(async () => {
    if (flight.current) {
      await flight.current;
      if (!pending.current) return;
    }
    const run = async () => {
      while (pending.current && current.current) {
        const value = pending.current;
        pending.current = null;
        setSaveState("保存中");
        try {
          const saved = await api<Result>(
            "/results/" + current.current.id,
            "PUT",
            { edited: value, revision: current.current.revision },
          );
          current.current = saved;
          setResult(saved);
          if (!pending.current) {
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
    };
    flight.current = run();
    try {
      await flight.current;
    } finally {
      flight.current = null;
    }
  }, [onError]);
  const load = useCallback(
    async (id: string | null) => {
      const generation = ++loadGeneration.current;
      await flush();
      if (id === current.current?.id) return;
      if (generation !== loadGeneration.current) return;
      setResult(null);
      setEdit(null);
      current.current = null;
      if (!id) {
        current.current = null;
        setResult(null);
        setEdit(null);
        return;
      }
      const value = await api<Result>("/results/" + id);
      if (generation !== loadGeneration.current) return;
      current.current = value;
      setResult(value);
      setEdit(value.edited);
      setSaveState("已保存");
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
      await flush();
      if (!current.current) return;
      const value = await api<Result>(
        "/results/" + current.current.id + "/history",
        "POST",
        { direction, revision: current.current.revision },
      );
      current.current = value;
      setResult(value);
      setEdit(value.edited);
    },
    [flush],
  );
  const reload = useCallback(async () => {
    if (!current.current) return;
    pending.current = null;
    const value = await api<Result>("/results/" + current.current.id);
    current.current = value;
    setResult(value);
    setEdit(value.edited);
    setSaveState("已保存");
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
  return { result, edit, change, load, flush, history, reload, saveState };
}
