import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import type { Result, Edit } from "./types";
import { recoveryClient, readRecovery, writeRecovery } from "./editorRecovery";
import { editableResult, savedEdit } from "./documentText";

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
  const decision = useRef<{ resultId: string; issueId: string; body: Record<string, unknown> } | null>(null);
  const reviewDraft = useRef<{ issueId: string; value: string } | null>(null);
  const client = useRef("");
  const persistRecovery = useCallback(() => {
    const value = current.current;
    if (!value) return;
    try {
      client.current ||= recoveryClient();
      writeRecovery(client.current, { resultId: value.id, revision: value.revision, edit: pending.current,
        review: reviewDraft.current, decision: decision.current, updated: Date.now() });
    } catch {
      onError("浏览器无法保存恢复副本；当前草稿仍在内存，请保存或下载草稿后再关闭页面。");
    }
  }, [onError]);
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
    if (decision.current) {
      const intent = decision.current;
      setSaveState("保存中");
      try {
        const saved = await api<Result>(`/results/${intent.resultId}/issues/${intent.issueId}/decision`, "POST", intent.body);
        if (current.current?.id !== intent.resultId) throw Error("决策保存期间结果已切换，请重新加载。");
        current.current = saved;
        setResult(saved);
        if (!pending.current) setEdit(editableResult(saved));
        decision.current = null;
        reviewDraft.current = null;
        persistRecovery();
        setSaveState("已保存");
      } catch (error) {
        setSaveState("保存失败");
        onError(String(error));
        throw error;
      }
    }
    while (pending.current && current.current) {
      const target = current.current;
      const generation = loadGeneration.current;
      const value = pending.current;
      pending.current = null;
      setSaveState("保存中");
      try {
        const saved = await api<Result>("/results/" + target.id, "PUT", {
          edited: savedEdit(value),
          revision: target.revision,
        });
        current.current = saved;
        persistRecovery();
        if (generation === loadGeneration.current) setResult(saved);
        if (!pending.current && generation === loadGeneration.current) {
          setEdit(editableResult(saved));
          setSaveState("已保存");
        }
      } catch (error) {
        pending.current = pending.current || value;
        persistRecovery();
        setSaveState("保存失败");
        onError(String(error));
        throw error;
      }
    }
  }, [onError, persistRecovery]);
  const flush = useCallback(() => enqueue(async () => {
    await savePending();
    if (reviewDraft.current) throw Error("快速校对中有未提交的手工修改，请先保存并继续，或清除手工修改。");
  }), [enqueue, savePending]);
  const load = useCallback(
    async (id: string | null) => {
      const generation = ++loadGeneration.current;
      await flush();
      if (generation !== loadGeneration.current) return;
      requested.current = id;
      setLoadError("");
      if (id === current.current?.id) {
        setResult(current.current);
        setEdit(editableResult(current.current));
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
        client.current ||= recoveryClient();
        const recovery = readRecovery(client.current, id);
        if (recovery) {
          pending.current = recovery.edit;
          decision.current = recovery.decision;
          reviewDraft.current = recovery.review;
          // Preserve the original expected revision of an unsaved draft so a
          // changed server result produces a conflict instead of an overwrite.
          if (recovery.edit && recovery.revision !== value.revision)
            current.current = { ...value, revision: recovery.revision };
          setEdit(recovery.edit || editableResult(value));
          setSaveState(recovery.decision ? "上次提交待核实" : recovery.revision !== value.revision ? "保存失败" : "已恢复未提交草稿");
          if (!recovery.decision && recovery.revision !== value.revision) onError("已恢复本地草稿，但服务器修订已变化。请下载草稿并核对差异后重新加载。");
        } else {
          setEdit(editableResult(value));
          setSaveState("已保存");
        }
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
      if (decision.current) { onError("上次校对提交尚未核实，请先重试该次提交，再继续修改。"); return; }
      setEdit(value);
      pending.current = value;
      persistRecovery();
      setSaveState("待保存");
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => {
        void flush().catch(() => {});
      }, 650);
    },
    [flush, persistRecovery, onError],
  );
  const history = useCallback(
    async (direction: number) => {
      const id = current.current?.id;
      const generation = loadGeneration.current;
      await enqueue(async () => {
        if (reviewDraft.current) throw Error("请先保存或清除快速校对中的手工修改。");
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
        setEdit(editableResult(value));
        setSaveState("已保存");
      });
    },
    [enqueue, savePending],
  );
  const decide = useCallback(async (issueId: string, body: Record<string, unknown>) => {
    const id = current.current?.id;
    const generation = loadGeneration.current;
    await enqueue(async () => {
      const retrying = decision.current?.body.request_id === body.request_id;
      if (decision.current && !retrying) throw Error("请先重试上次提交，再选择新的校对操作。");
      await savePending();
      if (retrying) { reviewDraft.current = null; return; }
      if (!id || current.current?.id !== id || generation !== loadGeneration.current)
        throw Error("校对目标已切换，请重新选择疑点。");
      decision.current = { resultId: id, issueId, body: { ...body, revision: current.current.revision } };
      persistRecovery();
      await savePending();
      reviewDraft.current = null;
    });
  }, [enqueue, savePending, persistRecovery]);
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
        decision.current = null;
        reviewDraft.current = null;
        current.current = value;
        persistRecovery();
        setResult(value);
        setEdit(editableResult(value));
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
  }, [enqueue, persistRecovery]);
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
            review_draft: reviewDraft.current,
            pending_decision: decision.current,
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
      if (pending.current || decision.current || reviewDraft.current || flight.current) {
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
  const setReviewDraft = useCallback((draft: { issueId: string; value: string } | null) => {
    reviewDraft.current = draft;
    persistRecovery();
  }, [persistRecovery]);
  const getReviewDraft = useCallback(() => reviewDraft.current, []);
  const getPendingDecision = useCallback(() => decision.current, []);
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
    decide,
    setReviewDraft,
    getReviewDraft,
    getPendingDecision,
  };
}
