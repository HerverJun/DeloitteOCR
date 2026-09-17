import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import {
  FileText,
  FolderPlus,
  Upload,
  Check,
  ChevronDown,
  Download,
  Undo2,
  Redo2,
  Copy,
  Search,
  X,
  LayoutList,
  Table2,
  Columns3,
  ScanLine,
  PenLine,
  CheckCircle2,
  AlertCircle,
  Settings,
  FolderOpen,
  ArrowRight,
  Plus,
  Power,
} from "lucide-react";
import { TaskQueue } from "./TaskQueue";
import { RecognitionBar, modes } from "./RecognitionBar";
import { BrandHeader } from "./BrandHeader";
import { WorkspaceLayout } from "./WorkspaceLayout";
import { PhotoList } from "./PhotoList";
import { DocumentTree } from "./DocumentTree";
import { documentReviewTab, type DocumentReviewTask } from "./DocumentReviewQueue";
import { StructureReview } from "./StructureReview";
import { MultimodalReview } from "./MultimodalReview";
import { multimodalTargetLabel } from "./multimodalTypes";
import { DocumentConflicts } from "./DocumentConflicts";
import {
  usePreference,
  readPreference,
  validMode,
  validEngine,
} from "./workspacePreferences";
import { api, request, download } from "./api";
import { ImageCanvas } from "./ImageCanvas";
import { TableEditor } from "./TableEditor";
import { ResultComparison } from "./ResultComparison";
import { QuickReview } from "./QuickReview";
import { GeometryPanel, type GeometryTarget } from "./GeometryPanel";
import type { Location } from "./RegionPreview";
import { FusionLauncher, type FusionRequest } from "./FusionLauncher";
import { adoptedResult, exportPhotos, reviewNames } from "./resultWorkflow";
import { useEditor } from "./useEditor";
import { changeDocumentTables, changeDocumentText, displayOffset, documentText } from "./documentText";
import { ProjectStorage } from "./ProjectStorage";
import { EnginePackages } from "./EnginePackages";
import { engineNames } from "./types";
import type {
  Project,
  ProjectState,
  Result,
  Photo,
  Engine,
  Task,
  Edit,
  ReviewIssue,
} from "./types";

export function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [project, setProject] = useState<ProjectState | null>(null);
  const selectedProject = useRef("");
  const projectGeneration = useRef(0);
  const [engines, setEngines] = useState<Record<string, Engine>>({});
  const [active, setActive] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [mode, setMode] = usePreference("ocr-ui-mode", "table", validMode);
  const [engine, setEngine] = usePreference(
    "ocr-ui-engine",
    modes.find(
      (m) => m.id === readPreference("ocr-ui-mode", "table", validMode),
    )!.engine,
    validEngine,
  );
  const [preprocess, setPreprocess] = useState("none");
  const [tab, setTab] = useState("table");
  const resultTabs = useRef(new Map<string, string>());
  const explicitImageTabs = useRef(new Map<string, string>());
  const chooseTab = (value: string) => {
    if (editor.getReviewDraft() && value !== "review") {
      onError("请先保存快速校对中的手工修改，或清除该修改。");
      return;
    }
    if (active) explicitImageTabs.current.set(active, value);
    if (editor.result) resultTabs.current.set(editor.result.id, value);
    setTab(value);
  };
  const [busy, setBusy] = useState(false);
  const activeActions = useRef(0);
  const [initial, setInitial] = useState(true);
  const [sessionExpired, setSessionExpired] = useState(false);
  const [connectionAttempt, setConnectionAttempt] = useState(0);
  const [message, setMessage] = useState("");
  const [error, setError] = useState(false);
  const [modalError, setModalError] = useState("");
  const [showQueue, setShowQueue] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [highlight, setHighlight] = useState<number | null>(null);
  const [reviewLocation, setReviewLocation] = useState<{ resultId: string; issue: ReviewIssue } | null>(null);
  const [reviewFocus, setReviewFocus] = useState<{ tableId?: string; tableIndex?: number; row: number; column: number; nonce: number } | null>(null);
  const [geometryTarget, setGeometryTarget] = useState<{ resultId: string; target: GeometryTarget } | null>(null);
  const [geometryLocation, setGeometryLocation] = useState<{ resultId: string; location: Location } | null>(null);
  const [geometryRefresh, setGeometryRefresh] = useState(0);
  const [manualBinding, setManualBinding] = useState<{ resultId: string; revision: number; versionId: string; target: GeometryTarget; nonce: number } | null>(null);
  const [newProject, setNewProject] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [rename, setRename] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [exportFormat, setExportFormat] = useState("xlsx");
  const [exportScope, setExportScope] = useState("current");
  const exportMany = exportScope !== "current";
  const [confirmedOnly, setConfirmedOnly] = useState(false);
  const [discardOpen, setDiscardOpen] = useState(false);
  const [reviewOnly, setReviewOnly] = useState(false);
  const [previews, setPreviews] = useState<Record<string, string>>({});
  const [confidenceLimit, setConfidenceLimit] = useState(0.8);
  const projectRevision = useRef<number | undefined>(undefined);
  const projectLatest = useRef<ProjectState | null>(null);
  const loadRetries = useRef(0);
  const [exportAggregate, setExportAggregate] = useState(false);
  const [diagnostics, setDiagnostics] = useState<any>(null);
  const [diagnosticOpen, setDiagnosticOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [searchIndex, setSearchIndex] = useState(0);
  const [compared, setCompared] = useState<Result[]>([]);
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const folderInput = useRef<HTMLInputElement>(null);
  const textInput = useRef<HTMLTextAreaElement>(null);
  const notify = useCallback((text: string, isError = false) => {
    setMessage(String(text).replace(/^Error: /, ""));
    setError(isError);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    if (!isError) toastTimer.current = setTimeout(() => setMessage(""), 5000);
  }, []);
  const onError = useCallback((text: string) => notify(text, true), [notify]);
  const editor = useEditor(onError);
  const textView = useMemo(() => editor.edit ? documentText(editor.edit).text : "", [editor.edit]);
  const [tablePositions, setTablePositions] = useState<Record<string, number>>({});
  const [documentReviewTarget, setDocumentReviewTarget] = useState<DocumentReviewTask | null>(null);
  const [structureFocus, setStructureFocus] = useState("");
  const [multimodalFocus, setMultimodalFocus] = useState("");
  const [reviewEntry, setReviewEntry] = useState(0);
  useEffect(() => {
    if (!documentReviewTarget?.result_id || editor.result?.id !== documentReviewTarget.result_id) return;
    const task = documentReviewTarget;
    if (task.kind === "fusion") { setTab("review"); setReviewEntry(n => n+1); }
    else if (documentReviewTab(task.kind) === "multimodal") { setTab("multimodal"); setMultimodalFocus(task.proposal_id || task.proposal_ids?.[0] || ""); }
    else { setTab("structure"); setStructureFocus(task.proposal_ids?.[0] || ""); }
    if (task.target.tables?.length) setReviewFocus({ tableIndex:task.target.tables[0], row:0, column:0, nonce:Date.now() });
    setDocumentReviewTarget(null);
  }, [documentReviewTarget, editor.result?.id]);
  const refresh = useCallback(async (id: string, incremental = false) => {
    if (!id) return;
    const suffix =
      incremental && projectRevision.current !== undefined
        ? `?since_revision=${projectRevision.current}`
        : "";
    const value = await api<ProjectState & { unchanged?: boolean }>(
      "/projects/" + id + suffix,
    );
    if (selectedProject.current === id) {
      if (
        value.revision !== undefined &&
        projectRevision.current !== undefined &&
        value.revision < projectRevision.current
      )
        return value;
      projectRevision.current = value.revision;
      if (value.unchanged) {
        if (
          projectLatest.current &&
          (JSON.stringify(projectLatest.current.queue) !==
            JSON.stringify(value.queue) ||
            JSON.stringify(projectLatest.current.fusion_queue) !== JSON.stringify(value.fusion_queue) ||
            projectLatest.current.disk?.low_space !== value.disk?.low_space)
        ) {
          projectLatest.current = {
            ...projectLatest.current,
            queue: value.queue,
            fusion_queue: value.fusion_queue,
            disk: value.disk,
          };
          setProject(projectLatest.current);
        }
      } else {
        projectLatest.current = value;
        setProject(value);
      }
    }
    return value;
  }, []);
  const chooseProject = useCallback(
    async (id: string) => {
      await editor.flush();
      ++projectGeneration.current;
      selectedProject.current = id;
      setProjectId(id);
      setProject(null);
      projectLatest.current = null;
      projectRevision.current = undefined;
      setPreviews({});
      setActive("");
      setSelected([]);
      await editor.load(null);
      localStorage.setItem("ocr-project", id);
      await refresh(id);
    },
    [editor.flush, editor.load, refresh],
  );
  useEffect(() => {
    const expired = () => setSessionExpired(true);
    const changed = () => setConnectionAttempt(n => n + 1);
    window.addEventListener("ocr-session-expired", expired);
    window.addEventListener("ocr-session-changed", changed);
    return () => {
      window.removeEventListener("ocr-session-expired", expired);
      window.removeEventListener("ocr-session-changed", changed);
    };
  }, []);
  useEffect(() => {
    let cancelled = false;
    api("/state")
      .then((value) => {
        if (cancelled) return;
        setSessionExpired(false);
        setMessage("");
        setProjects(value.projects);
        setEngines(value.engines);
        setReviewOnly(value.review_only === true);
        const saved = localStorage.getItem("ocr-project");
        const id =
          value.projects.find((p: Project) => p.id === saved)?.id ||
          value.projects[0]?.id;
        if (id) void (selectedProject.current === id ? refresh(id) : chooseProject(id)).catch(onError);
      })
      .catch(onError)
      .finally(() => { if (!cancelled) setInitial(false); });
    return () => { cancelled = true; };
  }, [connectionAttempt]);
  useEffect(() => {
    if (!projectId || sessionExpired) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        await refresh(projectId, true);
      } catch (error) {
        if (!cancelled) onError(String(error));
      }
      if (cancelled) return;
      const running = projectLatest.current?.tasks.some((t) =>
        ["running", "queued"].includes(t.status),
      );
      timer = setTimeout(poll, document.hidden ? 15000 : running ? 1200 : 4000);
    };
    timer = setTimeout(poll, 1200);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [projectId, refresh, onError, sessionExpired]);
  const photo = project?.images.find((p) => p.id === active) || null;
  const versions = project?.versions.filter((v) => v.image_id === active) || [];
  const version = versions.find((v) => v.id === photo?.active_version) || null;
  const tasks = project?.tasks.filter((t) => t.image_id === active) || [];
  const recognitionTasks = tasks.filter(t => t.kind !== "multimodal" && t.engine !== "reviewer");
  const finished = recognitionTasks.filter((t) => t.status === "succeeded" && t.result_id);
  const currentResult =
    (active && previews[active]) ||
    (photo && project ? adoptedResult(photo, project.tasks) : null);
  const loadedResultId = editor.result?.id;
  useEffect(() => {
    if (!editor.result) return;
    const id = editor.result.id;
    const initialTab =
      resultTabs.current.get(id) ||
      explicitImageTabs.current.get(active) ||
      (editor.result.edited.tables.length ? "table" : "text");
    const restoredTab = documentReviewTarget?.result_id === id ? documentReviewTab(documentReviewTarget.kind) : editor.getReviewDraft() && editor.result.original.fusion ? "review" : initialTab;
    resultTabs.current.set(id, restoredTab);
    setTab(restoredTab);
  }, [loadedResultId]);
  useEffect(() => {
    if (!active) return;
    void editor.load(currentResult).catch(onError);
  }, [active, currentResult]);
  useEffect(() => {
    loadRetries.current = 0;
  }, [active, currentResult]);
  useEffect(() => {
    if (
      editor.result ||
      editor.loadState !== "failed" ||
      loadRetries.current >= 2
    )
      return;
    const retry = setTimeout(
      () => {
        ++loadRetries.current;
        void editor.reload().catch(onError);
      },
      1000 * (loadRetries.current + 1),
    );
    return () => clearTimeout(retry);
  }, [editor.loadState, editor.result, editor.reload, currentResult, onError]);
  useEffect(() => {
    if (project && !active && project.images.length)
      setActive(project.images[0].id);
  }, [project, active]);
  useEffect(() => {
    setHighlight(null);
    setReviewFocus(null);
  }, [active, editor.result?.id]);
  const finishedKey = finished.map((t) => t.result_id).join(":");
  useEffect(() => {
    if (tab !== "compare") {
      setCompared([]);
      return;
    }
    let valid = true;
    Promise.all(finished.map((t) => api<Result>("/results/" + t.result_id)))
      .then((results) => {
        if (valid) setCompared(results);
      })
      .catch(onError);
    return () => {
      valid = false;
    };
  }, [tab, finishedKey, loadedResultId, editor.result?.revision]);
  const action = async (fn: () => Promise<unknown>) => {
    ++activeActions.current;
    setBusy(true);
    setModalError("");
    try {
      return await fn();
    } catch (e) {
      setModalError(String(e));
      onError(String(e));
    } finally {
      setBusy(--activeActions.current > 0);
    }
  };
  const selectPhoto = async (item: Photo) => {
    await editor.flush();
    setActive(item.id);
    setSearch("");
    setSearchIndex(0);
  };
  const ensureProject = async () => {
    if (selectedProject.current) return selectedProject.current;
    const value = await api<Project>("/projects", "POST", { name: "我的文档" });
    setProjects((old) => [value, ...old]);
    await chooseProject(value.id);
    return value.id;
  };
  const importFiles = async (files: File[]) => {
    if (!files.length) return;
    if (busy) {
      notify("请等待当前操作完成");
      return;
    }
    await action(async () => {
      const id = await ensureProject();
      const generation = projectGeneration.current;
      const body = new FormData();
      files.forEach((file) => body.append("files", file, file.name));
      const isDocument = files.some(file => /\.(pdf|tiff?)$/i.test(file.name));
      const response = await request("/projects/" + id + (isDocument ? "/documents" : "/images"), {
        method: "POST",
        body,
      });
      const value = await response.json();
      await refresh(id);
      if (
        value.images?.length &&
        selectedProject.current === id &&
        projectGeneration.current === generation
      ) {
        setActive(value.images[0].id);
        setSelected(value.images.map((p: Photo) => p.id));
      }
      notify(
        (isDocument ? `导入 ${value.documents?.length || 0} 个文档，请在文档树中选择页面` : `导入 ${value.images.length} 张图片`) +
          (selectedProject.current !== id ? "（已保存到原项目）" : "") +
          (value.errors.length
            ? `；${value.errors.map((x: any) => x.name + "：" + x.message).join("；")}`
            : ""),
        value.errors.length > 0,
      );
    });
  };
  const importDrop = async (event: React.DragEvent) => {
    event.preventDefault();
    setDragging(false);
    const output: File[] = [];
    const walk = async (entry: any): Promise<void> => {
      if (entry.isFile) {
        output.push(
          await new Promise<File>((resolve, reject) =>
            entry.file(resolve, reject),
          ),
        );
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        let entries: any[];
        do {
          entries = await new Promise<any[]>((resolve, reject) =>
            reader.readEntries(resolve, reject),
          );
          for (const item of entries) await walk(item);
        } while (entries.length);
      }
    };
    try {
      const items = Array.from(event.dataTransfer.items);
      if (items.some((item) => (item as any).webkitGetAsEntry?.()))
        for (const item of items) {
          const entry = (item as any).webkitGetAsEntry?.();
          if (entry) await walk(entry);
        }
      else output.push(...Array.from(event.dataTransfer.files));
      await importFiles(output);
    } catch (e) {
      onError(String(e));
    }
  };
  const run = async (ids?: string[]) => {
    await editor.flush();
    const photos =
      project?.images.filter((p) =>
        (ids || (selected.length && selected) || [active]).includes(p.id),
      ) || [];
    if (!photos.length) throw Error("请先导入或选择图片");
    await api("/projects/" + projectId + "/tasks", "POST", {
      version_ids: photos.map((p) => p.active_version),
      preprocess:
        preprocess === "contrast"
          ? [{ kind: "contrast", factor: 1.3 }]
          : preprocess === "rotate"
            ? [{ kind: "rotate", degrees: 90 }]
            : preprocess === "rotate-contrast"
              ? [
                  { kind: "rotate", degrees: 90 },
                  { kind: "contrast", factor: 1.3 },
                ]
              : [],
      engines: engine === "all" ? Object.keys(engines) : [engine],
    });
    setShowQueue(true);
    await refresh(projectId);
    notify(`已加入 ${photos.length * (engine === "all" ? 4 : 1)} 项任务`);
  };
  const transform = async (op: unknown) => {
    if (!version) return;
    await action(async () => {
      await editor.flush();
      const value = await api(
        "/versions/" + version.id + "/transform",
        "POST",
        op,
      );
      await refresh(projectId);
      if (value.queued) {
        setShowQueue(true);
        notify("去弯曲已加入队列，可暂停或取消");
      } else notify("已保存新图像版本，原图未改动");
    });
  };
  const changeVersion = async (id: string) => {
    await action(async () => {
      await editor.flush();
      await api("/images/" + active + "/version", "PUT", { version_id: id });
      await refresh(projectId);
    });
  };
  const region = async (box: number[]) => {
    if (!version) return;
    await action(async () => {
      await editor.flush();
      const v = await api("/versions/" + version.id + "/transform", "POST", {
        kind: "crop",
        box,
      });
      await api("/projects/" + projectId + "/tasks", "POST", {
        version_ids: [v.id],
        engines: engine === "all" ? Object.keys(engines) : [engine],
      });
      await refresh(projectId);
      setShowQueue(true);
      notify("区域已保存为新版本并加入识别");
    });
  };
  const previewResult = async (id: string, imageId = active) => {
    await editor.flush();
    setPreviews((old) => ({ ...old, [imageId]: id }));
    setActive(imageId);
  };
  const selectResult = async (id: string) => {
    await editor.flush();
    const target = editor.getCurrent()?.id === id ? editor.getCurrent()! : await api<Result>("/results/" + id);
    await api("/images/" + active + "/selection", "PUT", { result_id: id, revision: target.revision });
    setPreviews((old) => ({ ...old, [active]: id }));
    await refresh(projectId);
  };
  const runFusion = async (configuration: FusionRequest) => {
    await editor.flush();
    if (!project || !photo) throw Error("请先选择图片。");
    const fusion = { content_type: configuration.content_type, mode: configuration.mode };
    if (configuration.reuse) {
      await api(`/projects/${projectId}/fusion`, "POST", { ...fusion, result_ids: configuration.result_ids,
        expected_engines: configuration.engines, request_id: configuration.request_id });
    } else {
      const photos = selected.length ? project.images.filter(p => selected.includes(p.id)) : [photo];
      await api(`/projects/${projectId}/tasks`, "POST", { version_ids: photos.map(p => p.active_version),
        engines: configuration.engines, fusion, request_id: configuration.request_id,
        preprocess: preprocess === "contrast" ? [{ kind: "contrast", factor: 1.3 }] : preprocess === "rotate" ? [{ kind: "rotate", degrees: 90 }] : preprocess === "rotate-contrast" ? [{ kind: "rotate", degrees: 90 }, { kind: "contrast", factor: 1.3 }] : [] });
    }
    setShowQueue(true);
    await refresh(projectId);
    notify("已创建融合任务，完成后可在结果列表中预览和采用。");
  };
  const locateIssue = (issue: ReviewIssue, editPosition: boolean) => {
    if (editor.result) setReviewLocation({ resultId: editor.result.id, issue });
    if (!editPosition) return;
    if (issue.target.table_id) {
      setReviewFocus({ tableId: issue.target.table_id, row: issue.target.row || 0, column: issue.target.column || 0, nonce: Date.now() });
      chooseTab("table");
    } else {
      chooseTab("text");
      requestAnimationFrame(() => {
        textInput.current?.focus();
        if (editor.edit) {
          const chars = Array.from(editor.edit.text);
          textInput.current?.setSelectionRange(
            displayOffset(editor.edit, chars.slice(0, issue.target.start || 0).join("").length),
            displayOffset(editor.edit, chars.slice(0, issue.target.end ?? chars.length).join("").length));
        }
      });
    }
  };
  const setReview = async (status: string) => {
    await editor.flush();
    const resultId = editor.result?.id;
    if (!resultId || !photo) return;
    const latest = editor.getCurrent();
    if (!latest || latest.id !== resultId)
      throw Error("结果已切换，请重新核对后确认。");
    await api("/images/" + photo.id + "/review", "PUT", {
      status,
      result_id: resultId,
      revision: latest.revision,
      version_id: photo.active_version,
    });
    await refresh(projectId);
  };
  const queueAction = async (name: string, task?: Task) => {
    await api(
      "/projects/" + projectId + "/queue/" + name,
      "POST",
      task ? { task_ids: [task.id] } : {},
    );
    await refresh(projectId);
  };
  const exportResults = async () => {
    await editor.flush();
    let ids: string[] = [];
    if (exportMany) {
      const latest = await api<ProjectState>("/projects/" + projectId);
      const photos = exportPhotos(
        latest.images,
        selected,
        exportScope,
        confirmedOnly,
      );
      ids = photos
        .map((p) => adoptedResult(p, latest.tasks))
        .filter(Boolean) as string[];
      if (ids.length !== photos.length)
        throw new Error(
          `此范围 ${photos.length} 张图片中有 ${photos.length - ids.length} 张尚无可导出的结果，请等待识别或调整范围。`,
        );
    } else if (editor.result) ids = [editor.result.id];
    if (!ids.length)
      throw new Error("此范围没有可导出的结果，请调整范围或复核筛选。");
    await download(
      ids,
      exportFormat,
      exportMany && exportFormat === "xlsx" && exportAggregate,
      confirmedOnly,
    );
    setExportOpen(false);
    notify("导出文件已生成");
  };
  const findNext = () => {
    if (!editor.edit || !search) return;
    let i = textView.indexOf(search, searchIndex);
    if (i < 0) i = textView.indexOf(search);
    if (i < 0) {
      notify("未找到匹配文字");
      return;
    }
    textInput.current?.focus();
    textInput.current?.setSelectionRange(i, i + search.length);
    setSearchIndex(i + search.length);
  };
  const matchesVersion =
    editor.result?.original.project_image_version === version?.id;
  const blocks = matchesVersion ? editor.result?.original.blocks || [] : [];
  const completed =
    project?.tasks.filter((t) => t.status === "succeeded").length || 0;
  const pending =
    project?.tasks.filter((t) => ["queued", "running"].includes(t.status))
      .length || 0;
  const latestTask = recognitionTasks.at(-1);
  const attention =
    project?.tasks.filter((t) =>
      ["failed", "paused", "interrupted"].includes(t.status),
    ).length || 0;
  const queueHealthy = project?.queue.healthy !== false;
  const lastPending = useRef(0);
  useEffect(() => {
    if (lastPending.current > 0 && pending === 0 && attention === 0)
      setShowQueue(false);
    lastPending.current = pending;
  }, [pending, attention]);
  const targets = project
    ? exportPhotos(project.images, selected, exportScope, confirmedOnly)
    : [];
  const exportableCount = exportMany
    ? targets.filter((p) => adoptedResult(p, project?.tasks || [])).length
    : editor.result
      ? 1
      : 0;
  const currentAdopted =
    photo && project ? adoptedResult(photo, project.tasks) : null;
  const reviewCurrent =
    editor.saveState === "已保存" &&
    photo?.review_state?.result_id === editor.result?.id &&
    photo?.review_state?.revision === editor.result?.revision &&
    photo?.review_state?.version_id === photo?.active_version
      ? photo?.review_status || "pending"
      : "pending";
  const openExport = (scope: string) => {
    setModalError("");
    setExportScope(scope);
    setConfirmedOnly(false);
    setExportFormat(
      scope === "current" && !editor.edit?.tables.length ? "txt" : "xlsx",
    );
    setExportOpen(true);
  };
  const nextReview = () => {
    if (!project) return;
    const index = project.images.findIndex((p) => p.id === active);
    const ordered = [
      ...project.images.slice(index + 1),
      ...project.images.slice(0, index),
    ];
    const next = ordered.find(
      (p) => p.review_status !== "confirmed" && adoptedResult(p, project.tasks),
    );
    if (next) return selectPhoto(next);
    notify("其余已有结果均已确认");
  };
  return (
    <div
      className="app-shell"
      onDragOver={(e) => {
        e.preventDefault();
        if (e.dataTransfer.types.includes("Files")) setDragging(true);
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node))
          setDragging(false);
      }}
      onDrop={(e) => void importDrop(e)}
    >
      {sessionExpired && <div className="inline-warning" role="alert">
        <strong>工作台连接已失效</strong>
        <p>请从系统托盘重新打开工作台，或重新运行「启动工作台.cmd」。新启动链接会恢复连接，当前页面中的校对草稿会保留。</p>
        <Button onClick={() => setConnectionAttempt(n => n + 1)}>重新连接</Button>
      </div>}
      <BrandHeader>
        {!reviewOnly && (
          <EnginePackages
            onError={onError}
            onChange={async () => {
              const value = await api("/state");
              setEngines(value.engines);
              notify("引擎版本已更新，已有任务仍使用原版本");
            }}
          />
        )}
        <span className="offline-status">
          <span />
          {reviewOnly ? "仅校对与导出模式" : "仅在本机处理"}
        </span>
        <Button
          appearance="subtle"
          icon={<Settings size={17} />}
          onClick={() =>
            void action(async () => {
              setDiagnosticOpen(true);
              setDiagnostics(await api("/diagnostics"));
            })
          }
        >
          环境检查
        </Button>
      </BrandHeader>
      <WorkspaceLayout
        projectName={project?.project.name || ""}
        sidebar={
          <>
            <div className="project-control">
              <span className="section-label">项目资料</span>
              <Button
                aria-label="新建项目"
                appearance="subtle"
                size="small"
                icon={<FolderPlus size={17} />}
                onClick={() => {
                  setRename(false);
                  setModalError("");
                  setProjectName("");
                  setNewProject(true);
                }}
              />
            </div>
            <div className="project-picker">
              <FolderOpen size={17} />
              <select
                aria-label="当前项目"
                value={projectId}
                onChange={(e) =>
                  void action(() => chooseProject(e.target.value))
                }
              >
                <option value="" disabled>
                  选择或新建项目
                </option>
                {projects.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
            </div>
            <details className="project-menu">
              <summary>项目管理</summary>
              <div className="project-menu-content">
                <button
                  title="重命名项目"
                  disabled={!projectId}
                  onClick={() => {
                    setRename(true);
                    setModalError("");
                    setProjectName(project?.project.name || "");
                    setNewProject(true);
                  }}
                >
                  <PenLine size={14} /> 重命名项目
                </button>
                <ProjectStorage
                  id={projectId}
                  name={project?.project.name || ""}
                  onError={onError}
                  onDelete={async (confirmation) => {
                    await editor.flush();
                    const value = await api(
                      "/projects/" + projectId,
                      "DELETE",
                      {
                        confirmation,
                      },
                    );
                    const remaining = projects.filter(
                      (p) => p.id !== projectId,
                    );
                    setProjects(remaining);
                    await chooseProject(remaining[0]?.id || "");
                    notify(
                      value.cleanup_pending
                        ? "项目已删除，部分文件将在下次启动继续清理"
                        : "项目已清理",
                    );
                  }}
                />
              </div>
            </details>
            <div className="import-actions">
              <Button
                appearance="primary"
                icon={<Upload size={16} />}
                disabled={busy}
                onClick={() => fileInput.current?.click()}
              >
                导入图片 / PDF
              </Button>
              <Button
                title="导入文件夹"
                aria-label="导入文件夹"
                icon={<FolderPlus size={17} />}
                disabled={busy}
                onClick={() => folderInput.current?.click()}
              />
            </div>
            <input
              ref={fileInput}
              type="file"
              multiple
              accept=".pdf,.jpg,.jpeg,.png,.bmp,.tif,.tiff,.webp,.heic,.heif"
              hidden
              onChange={(e) => {
                void importFiles(Array.from(e.target.files || []));
                e.target.value = "";
              }}
            />
            <input
              ref={folderInput}
              type="file"
              multiple
              {...{ webkitdirectory: "", directory: "" }}
              hidden
              onChange={(e) => {
                void importFiles(Array.from(e.target.files || []));
                e.target.value = "";
              }}
            />
            {!!project?.documents?.some(d => d.kind !== "image") && <DocumentTree
              key={`documents-${projectId}`} documents={project.documents.filter(d => d.kind !== "image")}
              activeImage={active} reviewOnly={reviewOnly} beforeOpen={editor.flush}
              onOpen={async imageId => { await editor.flush(); setActive(imageId); setSearch(""); setSearchIndex(0); }}
              onReview={async task => {
                if (task.kind === "fusion" && task.result_id && task.issue_id) await api(`/results/${task.result_id}/issues/position`,"PUT",{issue_id:task.issue_id});
                setDocumentReviewTarget(task);
              }}
              onRefresh={() => refresh(projectId)} onError={onError} />}
            <PhotoList
              key={projectId}
              photos={(project?.images || []).filter(image => !image.document_kind || image.document_kind === "image")}
              tasks={project?.tasks || []}
              active={active}
              selected={selected}
              onSelect={(p) => void action(() => selectPhoto(p))}
              onSelection={setSelected}
            />
            <Button
              disabled={
                busy ||
                !selected.some((id) => {
                  const p = project?.images.find((p) => p.id === id);
                  return p && adoptedResult(p, project?.tasks || []);
                })
              }
              onClick={() => openExport("selected")}
              icon={<Download size={15} />}
            >
              导出所选 {selected.length ? `(${selected.length})` : ""}
            </Button>
            <button
              className={"queue-summary " + (showQueue ? "open" : "")}
              onClick={() => setShowQueue(!showQueue)}
            >
              <LayoutList size={18} />
              <span>
                任务队列
                <small>
                  {reviewOnly ? "仅校对与导出 · CPU 融合可用" : !queueHealthy
                    ? "队列异常，请查看恢复提示"
                    : attention
                      ? `${attention} 项失败或待恢复 · ${pending} 项处理中`
                      : pending
                        ? `${pending} 项等待或识别中`
                        : `${completed} 项已完成`}
                </small>
              </span>
              <ChevronDown
                size={16}
                style={{ transform: showQueue ? "rotate(180deg)" : undefined }}
              />
            </button>
            <div className="sidebar-footer">
              <Check size={13} />
              项目与校对自动保存在本机
            </div>
          </>
        }
        toolbar={
          <>
            {project?.fusion_queue?.healthy === false && <div className="inline-warning" role="alert">CPU 融合队列正在恢复。<Button size="small" disabled={busy} onClick={() => void action(async () => { await api("/fusion/queue/recover", "POST", {}); await refresh(projectId); })}>恢复 CPU 融合队列</Button></div>}
            {(reviewOnly || !queueHealthy) && (
              <div className="inline-warning" role="status">
                {reviewOnly
                  ? "当前可打开、校对和导出已有结果；识别需要完整模式。"
                  : `队列正在恢复：${typeof project?.queue.last_error === "string" ? project.queue.last_error : project?.queue.last_error?.message || "工作线程不可用，请检查日志。"}`}
                {!reviewOnly && (
                  <Button
                    size="small"
                    disabled={busy}
                    onClick={() =>
                      void action(async () => {
                        await api("/queue/recover", "POST", {});
                        await refresh(projectId);
                      })
                    }
                  >
                    重试恢复队列
                  </Button>
                )}
              </div>
            )}
            {project?.disk?.low_space && (
              <div className="inline-warning" role="alert">
                {project.disk.warning}
              </div>
            )}
            <RecognitionBar
              mode={mode}
              setMode={setMode}
              engine={engine}
              setEngine={setEngine}
              preprocess={preprocess}
              setPreprocess={setPreprocess}
              chooseTab={chooseTab}
              engines={engines}
              busy={busy}
              recognitionDisabled={reviewOnly || !queueHealthy}
              imageCount={project?.images.length || 0}
              selectedCount={selected.length}
              hasActive={!!active}
              onRun={() => void action(() => run())}
            />
            <FusionLauncher engines={engines} tasks={recognitionTasks} versionId={version?.id}
              selectedCount={selected.length} disabled={busy || !photo} recognitionDisabled={reviewOnly || !queueHealthy}
              onStart={runFusion} />
            <button
              className="workspace-queue-toggle"
              onClick={() => setShowQueue(!showQueue)}
              aria-expanded={showQueue}
            >
              任务队列 · {pending} 项处理中
              {attention
                ? ` · ${attention} 项待处理`
                : ` · ${completed} 项完成`}
            </button>
          </>
        }
        image={
          <ImageCanvas
            version={version}
            versions={versions}
            blocks={blocks}
            highlight={highlight}
            reviewLocation={tab === "review" && reviewLocation?.resultId === editor.result?.id ? reviewLocation?.issue.location : geometryLocation?.resultId === editor.result?.id ? geometryLocation?.location : undefined}
            manualBinding={manualBinding && manualBinding.resultId === editor.result?.id && manualBinding.versionId === version?.id ? `人工定位 ${manualBinding.nonce}` : undefined}
            onCancelBinding={() => setManualBinding(null)}
            onManualBind={async box => {
              try {
                if (!manualBinding || manualBinding.resultId !== editor.result?.id || manualBinding.versionId !== version?.id) throw Error("绑定对象已变化，请重新选择");
                await editor.flush();
                await api(`/results/${manualBinding.resultId}/geometry`, "POST", { source: "manual", revision: manualBinding.revision,
                  version_id: manualBinding.versionId, target: manualBinding.target,
                  polygon: [[box[0],box[1]],[box[2],box[1]],[box[2],box[3]],[box[0],box[3]]] });
                setManualBinding(null); setGeometryRefresh(n => n+1); notify("人工定位已保存");
              } catch (e) { onError(String(e)); }
            }}
            onVersion={(id) => void changeVersion(id)}
            onTransform={(op) => void transform(op)}
            onRegion={(box) => void region(box)}
            busy={busy}
            recognitionDisabled={reviewOnly || !queueHealthy}
          />
        }
        queue={
          showQueue &&
          project && (
            <TaskQueue
              project={project!}
              recognitionDisabled={reviewOnly || !queueHealthy}
              onClose={() => setShowQueue(false)}
              onAction={(name, task) =>
                void action(() => queueAction(name, task))
              }
              onView={(t) =>
                void action(async () => {
                  await editor.flush();
                  if (t.kind === "multimodal" || t.engine === "reviewer") {
                    const targetPhoto = project!.images.find(item => item.id === t.image_id);
                    const resultId = t.review_result_id || t.result_id || (targetPhoto ? adoptedResult(targetPhoto, project!.tasks) : null);
                    if (!resultId) throw Error("审校对应的识别结果不可用，请在原页面查看。");
                    await previewResult(resultId, t.image_id);
                    resultTabs.current.set(resultId, "multimodal");
                    explicitImageTabs.current.set(t.image_id, "multimodal");
                    setTab("multimodal");
                  } else if (t.result_id) {
                    await previewResult(t.result_id, t.image_id);
                  } else {
                    setActive(t.image_id);
                    await api("/images/" + t.image_id + "/version", "PUT", {
                      version_id: t.result_version_id,
                    });
                    await refresh(projectId);
                  }
                })
              }
            />
          )
        }
      >
        <section className="result-workspace" aria-label="识别与校对结果">
          <div className="result-heading">
            <div className="result-title">
              <div>
                <h2>校对结果</h2>
                <span className="result-filename" title={photo?.name}>
                  {photo
                    ? `${photo.name} · ${(project?.images.findIndex((p) => p.id === active) || 0) + 1}/${project?.images.length}`
                    : "等待导入资料"}
                </span>
              </div>
              <span
                className={
                  "save-status " +
                  (editor.saveState === "保存失败" ? "failed" : "")
                }
              >
                {editor.saveState === "保存失败" ? (
                  <AlertCircle size={14} />
                ) : editor.saveState === "已保存" ? (
                  <CheckCircle2 size={14} />
                ) : (
                  <Spinner size="tiny" />
                )}
                {editor.result ? editor.saveState : "等待识别"}
              </span>
            </div>
            {finished.length > 0 && (
              <div className="result-selector">
                <select
                  aria-label="当前识别结果"
                  value={editor.result?.id || ""}
                  onChange={(e) =>
                    void action(() => previewResult(e.target.value))
                  }
                >
                  {finished.map((t, i) => (
                    <option key={t.id} value={t.result_id!}>
                      {engineNames[t.engine]} · 结果 {i + 1}
                      {t.result_id === photo?.selected_result
                        ? " · 已采用"
                        : ""}
                    </option>
                  ))}
                </select>
                {editor.result && (
                  <Button
                    size="small"
                    disabled={busy || editor.result.id === currentAdopted}
                    onClick={() =>
                      void action(() => selectResult(editor.result!.id))
                    }
                  >
                    {editor.result.id === currentAdopted
                      ? "已采用"
                      : "采用此预览"}
                  </Button>
                )}
                {editor.result && (
                  <span>
                    {typeof editor.result.original.elapsed_seconds === "number" ? `${editor.result.original.elapsed_seconds.toFixed(2)} 秒` : "耗时未记录"}
                  </span>
                )}
              </div>
            )}
          </div>
          <div className="result-view-bar">
            <div
              className="result-tabs"
              role="tablist"
              aria-label="结果视图"
              onKeyDown={(e) => {
                const choices = editor.result?.original.fusion ? ["table", "structure", "text", "compare", "multimodal", "review"] : ["table", "structure", "text", "compare", "multimodal"];
                if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key))
                  return;
                e.preventDefault();
                const index =
                  e.key === "Home"
                    ? 0
                    : e.key === "End"
                      ? choices.length-1
                      : (choices.indexOf(tab) +
                          (e.key === "ArrowRight" ? 1 : choices.length-1)) % choices.length;
                chooseTab(choices[index]);
                (
                  e.currentTarget.querySelectorAll("button")[
                    index
                  ] as HTMLButtonElement
                ).focus();
              }}
            >
              <button
                role="tab"
                aria-selected={tab === "table"}
                tabIndex={tab === "table" ? 0 : -1}
                aria-controls="result-content"
                id="tab-table"
                className={tab === "table" ? "active" : ""}
                onClick={() => chooseTab("table")}
              >
                <Table2 size={15} />
                表格
                {editor.edit?.tables.length
                  ? ` ${editor.edit.tables.length}`
                  : ""}
              </button>
              <button role="tab" aria-selected={tab === "structure"} tabIndex={tab === "structure" ? 0 : -1}
                aria-controls="result-content" id="tab-structure" className={tab === "structure" ? "active" : ""}
                onClick={() => chooseTab("structure")}>结构复核</button>
              <button
                role="tab"
                aria-selected={tab === "text"}
                tabIndex={tab === "text" ? 0 : -1}
                aria-controls="result-content"
                id="tab-text"
                className={tab === "text" ? "active" : ""}
                onClick={() => chooseTab("text")}
              >
                <FileText size={15} />
                文字
              </button>
              <button
                role="tab"
                aria-selected={tab === "compare"}
                tabIndex={tab === "compare" ? 0 : -1}
                aria-controls="result-content"
                id="tab-compare"
                className={tab === "compare" ? "active" : ""}
                onClick={() => chooseTab("compare")}
              >
                <Columns3 size={15} />
                模型对比
              </button>
              <button role="tab" aria-selected={tab === "multimodal"} tabIndex={tab === "multimodal" ? 0 : -1}
                aria-controls="result-content" id="tab-multimodal" className={tab === "multimodal" ? "active" : ""}
                onClick={() => chooseTab("multimodal")}><ScanLine size={15} />视觉审校</button>
              {editor.result?.original.fusion && <button role="tab" aria-selected={tab === "review"}
                tabIndex={tab === "review" ? 0 : -1} aria-controls="result-content" id="tab-review"
                className={tab === "review" ? "active" : ""} onClick={() => chooseTab("review")}>快速校对</button>}
            </div>
            {editor.result && editor.edit && (
              <div className="edit-toolbar">
                <Button
                  size="small"
                  appearance="subtle"
                  title="撤销"
                  aria-label="撤销"
                  icon={<Undo2 size={16} />}
                  disabled={!editor.result.can_undo || busy}
                  onClick={() => void action(() => editor.history(-1))}
                />
                <Button
                  size="small"
                  appearance="subtle"
                  title="重做"
                  aria-label="重做"
                  icon={<Redo2 size={16} />}
                  disabled={!editor.result.can_redo || busy}
                  onClick={() => void action(() => editor.history(1))}
                />
                <span className="edit-toolbar-divider" />
                <Button
                  size="small"
                  appearance="subtle"
                  icon={<Copy size={15} />}
                  disabled={busy}
                  onClick={() =>
                    void action(async () => {
                      const id = editor.result!.id;
                      await editor.flush();
                      const response = await request("/export", {
                        method: "POST",
                        body: JSON.stringify({
                          result_ids: [id],
                          format: "txt",
                        }),
                      });
                      await navigator.clipboard.writeText(
                        await response.text(),
                      );
                      notify("文字已复制");
                    })
                  }
                >
                  复制
                </Button>
              </div>
            )}
          </div>
          <div
            id="result-content"
            className="result-content"
            role="tabpanel"
            aria-labelledby={`tab-${tab}`}
          >
            {!editor.result || !editor.edit ? (
              <div className="empty-panel">
                <ScanLine size={34} />
                <h3>
                  {editor.loadState === "failed"
                    ? "结果加载失败"
                    : editor.loadState === "loading"
                      ? "正在加载已保存结果"
                      : !photo
                        ? "导入第一份资料"
                        : latestTask?.status === "failed"
                          ? "识别未完成"
                          : latestTask &&
                              ["paused", "interrupted"].includes(
                                latestTask.status,
                              )
                            ? "任务等待继续"
                            : pending &&
                                tasks.some((t) =>
                                  ["running", "queued"].includes(t.status),
                                )
                              ? "正在识别图片"
                              : latestTask?.status === "cancelled"
                                ? "任务已取消"
                                : "图片已准备好"}
                </h3>
                <p>
                  {editor.loadState === "failed"
                    ? editor.loadError
                    : !photo
                      ? "导入图片或整个文件夹，开始整理文档。"
                      : latestTask?.error ||
                        (reviewOnly
                          ? "可打开已有结果进行校对与导出。"
                          : "识别完成后，可校对文字和表格。")}
                </p>
                {editor.loadState === "failed" ? (
                  <Button
                    onClick={() => void action(editor.reload)}
                    disabled={busy}
                  >
                    重试加载
                  </Button>
                ) : editor.loadState === "loading" ? (
                  <Spinner size="small" />
                ) : !photo ? (
                  <div className="empty-actions">
                    <Button
                      appearance="primary"
                      icon={<Upload size={16} />}
                      disabled={busy}
                      onClick={() => fileInput.current?.click()}
                    >
                      导入图片
                    </Button>
                    <Button
                      disabled={busy}
                      onClick={() => folderInput.current?.click()}
                    >
                      导入文件夹
                    </Button>
                  </div>
                ) : latestTask &&
                  ["failed", "cancelled", "paused", "interrupted"].includes(
                    latestTask.status,
                  ) ? (
                  <Button
                    disabled={busy || reviewOnly || !queueHealthy}
                    onClick={() =>
                      void action(() =>
                        queueAction(
                          ["failed", "cancelled"].includes(latestTask.status)
                            ? "retry"
                            : "resume",
                          latestTask,
                        ),
                      )
                    }
                  >
                    {["failed", "cancelled"].includes(latestTask.status)
                      ? "重试此任务"
                      : "继续此任务"}
                  </Button>
                ) : recognitionTasks.some((t) =>
                    ["running", "queued"].includes(t.status),
                  ) ? (
                  <Button onClick={() => setShowQueue(true)}>
                    查看任务进度
                  </Button>
                ) : (
                  <Button
                    appearance="primary"
                    disabled={busy || reviewOnly || !queueHealthy}
                    onClick={() => void action(() => run([active]))}
                  >
                    识别当前图片
                  </Button>
                )}
              </div>
            ) : (
              <>
                {editor.saveState === "保存失败" && (
                  <div className="inline-warning" role="alert">
                    校对尚未保存，草稿仍保留。请重试保存或下载副本。
                    <Button size="small" onClick={editor.downloadDraft}>
                      下载草稿副本
                    </Button>
                    <Button
                      size="small"
                      onClick={() => void action(editor.flush)}
                    >
                      重试保存
                    </Button>
                    <Button size="small" onClick={() => { setModalError(""); setDiscardOpen(true); }}>
                      放弃本次修改并重新加载
                    </Button>
                  </div>
                )}
                {!!editor.result.original.warnings?.length && (
                  <details className="result-warnings">
                    <summary>
                      识别结果有 {editor.result.original.warnings.length}{" "}
                      项格式提示，文字与原始输出已保留
                    </summary>
                    {editor.result.original.warnings.map((w, i) => (
                      <p key={i}>{w.message}</p>
                    ))}
                  </details>
                )}
                <div className={`review-bar ${reviewCurrent}`}>
                  <span className="review-indicator">
                    {reviewCurrent === "confirmed" ? (
                      <CheckCircle2 size={14} />
                    ) : reviewCurrent === "question" ? (
                      <AlertCircle size={14} />
                    ) : (
                      <PenLine size={14} />
                    )}
                    复核：{reviewNames[reviewCurrent]}
                    {editor.result.id !== currentAdopted
                      ? " · 正在预览未采用结果"
                      : ""}
                  </span>
                  <Button
                    size="small"
                    icon={<Check size={14} />}
                    disabled={
                      busy ||
                      !matchesVersion ||
                      editor.result.id !== currentAdopted
                    }
                    onClick={() => void action(() => setReview("confirmed"))}
                  >
                    确认此结果
                  </Button>
                  <details>
                    <summary>复核操作</summary>
                    <div>
                      <Button
                        size="small"
                        disabled={
                          busy ||
                          !matchesVersion ||
                          editor.result.id !== currentAdopted
                        }
                        onClick={() => void action(() => setReview("question"))}
                      >
                        标记有疑问
                      </Button>
                      <Button
                        size="small"
                        disabled={busy || editor.result.id !== currentAdopted}
                        onClick={() => void action(() => setReview("pending"))}
                      >
                        设为待校对
                      </Button>
                      <Button
                        size="small"
                        disabled={busy}
                        onClick={() =>
                          void action(async () => {
                            await nextReview();
                          })
                        }
                      >
                        下一张待校对
                      </Button>
                    </div>
                  </details>
                </div>
                {!matchesVersion && (
                  <div className="version-warning">
                    当前结果对应另一图像版本。
                    <button
                      onClick={() =>
                        void changeVersion(
                          editor.result!.original.project_image_version!,
                        )
                      }
                    >
                      定位结果图片
                    </button>
                  </div>
                )}
                {["table", "text"].includes(tab) && <div className="multimodal-entry">
                  <div><strong>对照原图再核对一轮</strong><span>{geometryTarget?.resultId === editor.result.id ? `已选中：${multimodalTargetLabel(geometryTarget.target)}` : "可选中文字或表格单元格，也可复核本页。"}</span></div>
                  <Button size="small" icon={<ScanLine size={15} />} disabled={busy} onClick={() => chooseTab("multimodal")}>视觉审校</Button>
                </div>}
                {tab === "text" && (
                  <div className="text-editor">
                    <div className="text-search">
                      <Search size={15} />
                      <input
                        aria-label="搜索识别文字"
                        placeholder="查找文字"
                        value={search}
                        onChange={(e) => {
                          setSearch(e.target.value);
                          setSearchIndex(0);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") findNext();
                        }}
                      />
                      <button aria-label="下一个匹配" onClick={findNext}>
                        <ArrowRight size={16} />
                      </button>
                    </div>
                    <textarea
                      ref={textInput}
                      aria-label="校对文字"
                      readOnly={busy}
                      spellCheck={false}
                      wrap={editor.edit.tables.length || editor.edit.text_sources?.length ? "off" : "soft"}
                      className={editor.edit.tables.length || editor.edit.text_sources?.length ? "tabular-text" : undefined}
                      value={textView}
                      onSelect={e => {
                        const { selectionStart: start, selectionEnd: end } = e.currentTarget;
                        if (!editor.edit || !editor.result || start === end) return;
                        const segment = documentText(editor.edit).segments.find(s => start >= s.start && end <= s.end && s.kind !== "separator");
                        if (segment?.kind === "text") {
                          const rawStart = segment.rawStart! + start-segment.start, rawEnd = segment.rawStart! + end-segment.start;
                          setGeometryTarget({ resultId: editor.result.id, target: { kind: "text", start: Array.from(editor.edit.text.slice(0,rawStart)).length, end: Array.from(editor.edit.text.slice(0,rawEnd)).length } });
                        } else if (segment?.kind === "cell" && segment.table !== undefined && segment.cell !== undefined) {
                          const c = editor.edit.tables[segment.table].cells[segment.cell];
                          setGeometryTarget({ resultId: editor.result.id, target: { kind: "cell", table: segment.table, row: c.row, column: c.column } });
                        } else setGeometryTarget(null);
                      }}
                      onChange={(e) => {
                        try { editor.change(changeDocumentText(editor.edit!, e.target.value)); setGeometryTarget(null); }
                        catch (error) { onError(String(error)); }
                      }}
                    />
                    <details className="text-blocks">
                      <summary>
                        定位文字区域与置信度 (
                        {editor.result.original.blocks.length})
                      </summary>
                      <p>
                        分数来自 {engineNames[editor.result.original.engine]}
                        ，不代表统一准确率；无分数时为未知。
                      </p>
                      <label>
                        待核对阈值{" "}
                        <input
                          type="number"
                          aria-label="文字置信度阈值"
                          min={0}
                          max={1}
                          step={0.05}
                          value={confidenceLimit}
                          onChange={(e) =>
                            setConfidenceLimit(
                              Math.max(0, Math.min(1, Number(e.target.value))),
                            )
                          }
                        />
                      </label>
                      <Button
                        size="small"
                        onClick={() => {
                          const candidates = editor
                            .result!.original.blocks.map((b, i) => ({ b, i }))
                            .filter(
                              ({ b }) =>
                                typeof b.confidence === "number" &&
                                b.confidence < confidenceLimit &&
                                b.polygon,
                            );
                          const next =
                            candidates.find((c) => c.i > (highlight ?? -1)) ||
                            candidates[0];
                          if (next) {
                            if (!matchesVersion)
                              void changeVersion(
                                editor.result!.original.project_image_version!,
                              );
                            setHighlight(next.i);
                          } else
                            notify("没有带坐标的低分区域，可逐项查看原文。");
                        }}
                      >
                        下一处待核对
                      </Button>
                      {editor.result.original.blocks.map((block, i) => (
                        <button
                          key={i}
                          disabled={!block.polygon}
                          className={highlight === i ? "active" : ""}
                          onClick={() => {
                            if (!matchesVersion)
                              void changeVersion(
                                editor.result!.original.project_image_version!,
                              );
                            setHighlight(i);
                          }}
                        >
                          <span>{i + 1}</span>
                          {block.text.slice(0, 90)}
                          <small>
                            {typeof block.confidence === "number"
                              ? `${Math.round(block.confidence * 100)}%${block.confidence < confidenceLimit ? " · 待核对" : ""}`
                              : "置信度未知"}
                          </small>
                        </button>
                      ))}
                    </details>
                    <div className="text-count">
                      {textView.length} 字符
                      <span>UTF-8 · 保留原始编号</span>
                    </div>
                  </div>
                )}
                {tab === "table" && (
                  <TableEditor
                    key={`table-${editor.result.id}`}
                    activeIndex={tablePositions[editor.result.id] || 0}
                    onIndexChange={index => setTablePositions(old => ({ ...old, [editor.result!.id]: index }))}
                    disabled={busy}
                    focusTarget={reviewFocus}
                    onCellFocus={(table, row, column) => setGeometryTarget({ resultId: editor.result!.id, target: { kind: "cell", table, row, column } })}
                    tables={editor.edit.tables}
                    onError={onError}
                    onChange={(tables) =>
                      editor.change(changeDocumentTables(editor.edit!, tables))
                    }
                  />
                )}
                {tab === "review" && editor.result.original.fusion && photo && <QuickReview
                  key={`review-${editor.result.id}-${reviewEntry}`} result={editor.result} versionId={photo.active_version}
                  version={version} geometryRefresh={geometryRefresh}
                  adopted={currentAdopted === editor.result.id} busy={busy}
                  onDraft={editor.setReviewDraft}
                  getDraft={editor.getReviewDraft}
                  getPendingDecision={editor.getPendingDecision}
                  onLocate={locateIssue}
                  onDecision={async (issueId, body) => {
                    ++activeActions.current; setBusy(true);
                    try { await editor.decide(issueId, body); await refresh(projectId); }
                    finally { setBusy(--activeActions.current > 0); }
                  }}
                  onConfirm={() => void action(() => setReview("confirmed"))} />}
                {version && ["structure", "review"].includes(tab) && <StructureReview key={`structure-${editor.result.id}`} result={editor.result} version={version}
                  busy={busy} refreshKey={geometryRefresh} focusId={structureFocus} getPendingDecision={editor.getPendingDecision}
                  onPrepare={async () => { await editor.flush(); const r = editor.getCurrent(); if (!r) throw Error("请选择结果"); return r; }}
                  onDecision={async (id, body) => {
                    ++activeActions.current; setBusy(true);
                    try { await editor.decide(id,body); await refresh(projectId); } finally { setBusy(--activeActions.current > 0); }
                  }}
                  onUpdated={() => { setGeometryRefresh(n => n+1); void refresh(projectId); }}
                  onLocate={location => setGeometryLocation({ resultId:editor.result!.id,location })}
                  onManual={(table,row,column) => { setReviewFocus({tableIndex:table,row,column,nonce:Date.now()}); chooseTab("table"); }} />}
                {version && tab === "multimodal" && <MultimodalReview key={`multimodal-${editor.result.id}`} result={editor.result} version={version}
                  busy={busy || !matchesVersion} adopted={currentAdopted === editor.result.id} refreshKey={geometryRefresh} focusId={multimodalFocus}
                  target={geometryTarget?.resultId === editor.result.id ? geometryTarget.target : null}
                  beforeSubmit={async () => { await editor.flush(); const current = editor.getCurrent(); if (!current) throw Error("请先选择识别结果"); return current; }}
                  getPendingDecision={editor.getPendingDecision}
                  onDecision={async (proposalId, body) => {
                    ++activeActions.current; setBusy(true);
                    try {
                      await editor.decide(proposalId, { ...body, decision_kind: "multimodal" });
                      const current = editor.getCurrent();
                      if (!current) throw Error("审校后识别结果不可用，请重新加载。");
                      return current;
                    } finally { setBusy(--activeActions.current > 0); }
                  }}
                  onResult={async () => { await refresh(projectId); notify("审校决定已保存"); }}
                  onQueued={async () => { await refresh(projectId); }}
                  onLocate={location => setGeometryLocation({ resultId: editor.result!.id, location })} />}
                {version && ["table", "text", "review", "structure"].includes(tab) && <GeometryPanel
                  key={`geometry-${editor.result.id}`} result={editor.result} version={version} tasks={tasks} refreshKey={geometryRefresh}
                  disabled={busy || reviewOnly || !queueHealthy}
                  target={geometryTarget?.resultId === editor.result.id ? geometryTarget.target : null}
                  onLocate={location => setGeometryLocation({ resultId: editor.result!.id, location })}
                  onPrepare={async () => { await editor.flush(); const r = editor.getCurrent(); if (!r) throw Error("请先选择结果"); return r; }}
                  onUpdated={() => { setGeometryRefresh(n => n+1); void refresh(projectId); }}
                  onBind={target => void action(async () => {
                    await editor.flush(); const r = editor.getCurrent(); if (!r) return;
                    setManualBinding({ resultId: r.id, revision: r.revision, versionId: version.id, target, nonce: Date.now() });
                    notify("请在左侧原图拖动框选文字范围，然后点击应用");
                  })} />}
                {version && editor.result.original.origin === "document" && <DocumentConflicts result={editor.result} onLocate={polygon => setGeometryLocation({resultId:editor.result!.id,location:{level:"region",polygon,version_id:version.id,reason:"页面内容待核对"}})} beforeSave={async () => {
                  await editor.flush(); const r = editor.getCurrent(); if (!r) throw Error("结果已切换"); return r;
                }} />}
                {tab === "compare" && (
                  <ResultComparison
                    results={compared}
                    tasks={recognitionTasks}
                    versions={versions}
                    selectedResultId={currentAdopted}
                    reviewStatus={photo?.review_status}
                    reviewedResultId={photo?.review_state?.result_id}
                    reviewedRevision={photo?.review_state?.revision}
                    onAdopt={(id) => void action(() => selectResult(id))}
                    adoptingDisabled={busy}
                  />
                )}
              </>
            )}
          </div>
          <footer className="result-footer">
            <span>
              {editor.edit?.tables.length
                ? `${editor.edit.tables.length} 个表格可导出`
                : "可导出文字与原始 JSON"}
            </span>
            <Button
              appearance="primary"
              icon={<Download size={16} />}
              disabled={
                busy ||
                (!editor.result &&
                  !project?.images.some((p) => adoptedResult(p, project.tasks)))
              }
              onClick={() =>
                openExport(
                  editor.result
                    ? "current"
                    : selected.length
                      ? "selected"
                      : "project",
                )
              }
            >
              导出结果
            </Button>
          </footer>
        </section>
      </WorkspaceLayout>
      {message && (
        <div role="alert" className={"toast " + (error ? "error" : "")}>
          <span>
            {error ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}
          </span>
          {error ? (
            <div className="error-message">
              <p>操作未完成，请检查以下原因后重试。</p>
              <details open>
                <summary>详细原因</summary>
                <p>{message}</p>
              </details>
            </div>
          ) : (
            <p>{message}</p>
          )}
          <button aria-label="关闭提示" onClick={() => setMessage("")}>
            <X size={17} />
          </button>
        </div>
      )}
      {dragging && (
        <div className="drop-overlay">
          <Upload size={38} />
          <h2>松开以导入图片</h2>
          <p>也可以拖入整个文件夹</p>
        </div>
      )}
      {initial && (
        <div className="boot-overlay">
          <Spinner label="正在打开本机工作台" />
        </div>
      )}
      <Dialog
        open={newProject}
        onOpenChange={(_, data) => setNewProject(data.open)}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>{rename ? "重命名项目" : "新建项目"}</DialogTitle>
            <DialogContent>
              {modalError && <div className="inline-warning" role="alert">{modalError}</div>}
              <label className="dialog-field">
                项目名称
                <input
                  autoFocus
                  aria-label="项目名称"
                  value={projectName}
                  onChange={(e) => setProjectName(e.target.value)}
                  maxLength={120}
                  placeholder="例如：采购凭证 · 九月"
                />
              </label>
              <p className="dialog-description">
                原图、识别结果和校对记录自动保存在本机。
              </p>
            </DialogContent>
            <DialogActions>
              <Button onClick={() => setNewProject(false)}>取消</Button>
              <Button
                appearance="primary"
                disabled={!projectName.trim() || busy}
                onClick={() =>
                  void action(async () => {
                    const value = await api<Project>(
                      rename ? "/projects/" + projectId : "/projects",
                      rename ? "PATCH" : "POST",
                      { name: projectName },
                    );
                    setProjects((old) =>
                      rename
                        ? old.map((p) => (p.id === value.id ? value : p))
                        : [value, ...old],
                    );
                    await chooseProject(value.id);
                    setNewProject(false);
                  })
                }
              >
                {rename ? "保存名称" : "创建项目"}
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
      <Dialog
        open={exportOpen}
        onOpenChange={(_, data) => setExportOpen(data.open)}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>导出校对结果</DialogTitle>
            <DialogContent>
              {modalError && <div className="inline-warning" role="alert">{modalError}</div>}
              <label className="dialog-field">
                文件格式
                <select
                  aria-label="导出格式"
                  value={exportFormat}
                  onChange={(e) => setExportFormat(e.target.value)}
                >
                  <option value="xlsx">Excel 工作簿 (.xlsx)</option>
                  <option value="txt">纯文本 (.txt)</option>
                  <option value="md">Markdown (.md)</option>
                  <option value="json">完整结果与原始输出 (.json)</option>
                  <option value="pdf">可搜索 PDF（按文档输出）</option>
                </select>
              </label>
              <label className="dialog-field">
                导出范围
                <select
                  aria-label="导出范围"
                  value={exportScope}
                  onChange={(e) => setExportScope(e.target.value)}
                >
                  <option value="current" disabled={!editor.result}>
                    当前预览结果
                  </option>
                  <option value="selected" disabled={!selected.length}>
                    所选图片的采用结果 ({selected.length} 张)
                  </option>
                  <option value="project">
                    整个项目的采用结果 ({project?.images.length || 0} 张)
                  </option>
                </select>
              </label>
              <label className="export-scope">
                <input
                  type="checkbox"
                  checked={confirmedOnly}
                  onChange={(e) => setConfirmedOnly(e.target.checked)}
                />
                仅导出已确认结果
              </label>
              <p className="dialog-description">
                {exportMany
                  ? `范围内 ${targets.length} 张，可导出 ${exportableCount} 张${targets.length > exportableCount ? `；${targets.length - exportableCount} 张尚无结果，请调整范围。` : "。"}`
                  : `当前预览：${photo?.name || ""} · ${editor.result?.id === currentAdopted ? "已采用" : "未采用"}。批量导出使用每张图片的采用结果。`}
              </p>
              {exportFormat === "xlsx" && exportMany && (
                <label className="export-scope">
                  <input
                    type="checkbox"
                    checked={exportAggregate}
                    onChange={(e) => setExportAggregate(e.target.checked)}
                  />
                  汇总为一个工作簿
                </label>
              )}
              <p className="dialog-description">
                默认每张图片独立文件，多张打包为 ZIP。Excel
                中每个表格单独一页，保留合并关系、前导零与长编号。导出前会先保存当前校对。
              </p>
              {editor.result?.original.fusion && <p className="dialog-description">融合结果会与来源证据、决策历史一起打包为 ZIP，所有文件使用同一已保存修订。未处理、有疑问、过期和人工确认状态列入来源清单。</p>}
            </DialogContent>
            <DialogActions>
              <Button onClick={() => setExportOpen(false)}>取消</Button>
              <Button
                appearance="primary"
                disabled={
                  busy ||
                  !exportableCount ||
                  (exportMany && exportableCount !== targets.length) ||
                  (!exportMany &&
                    confirmedOnly &&
                    (reviewCurrent !== "confirmed" ||
                      editor.result?.id !== currentAdopted))
                }
                icon={<Download size={16} />}
                onClick={() => void action(exportResults)}
              >
                保存文件
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
      <Dialog
        open={discardOpen}
        onOpenChange={(_, data) => !busy && setDiscardOpen(data.open)}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>放弃本次未保存修改</DialogTitle>
            <DialogContent>
              {modalError && <div className="inline-warning" role="alert">{modalError}</div>}
              <p>
                服务器结果读取成功后，本地未保存的文字和表格将被替换。可以先下载草稿副本；读取失败时仍保留草稿。
              </p>
            </DialogContent>
            <DialogActions>
              <Button disabled={busy} onClick={() => setDiscardOpen(false)}>
                保留修改
              </Button>
              <Button onClick={editor.downloadDraft}>下载草稿副本</Button>
              <Button
                disabled={busy}
                onClick={() =>
                  void action(async () => {
                    await editor.reload();
                    setDiscardOpen(false);
                  })
                }
              >
                放弃修改并加载
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
      <Dialog
        open={diagnosticOpen}
        onOpenChange={(_, data) => setDiagnosticOpen(data.open)}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>运行环境</DialogTitle>
            <DialogContent>
              {modalError && <div className="inline-warning" role="alert">{modalError}</div>}
              {diagnostics ? (
                <>
                  <p>{diagnostics.passed ? "环境检查通过" : "发现环境问题"}</p>
                  <pre className="diagnostics">{diagnostics.details}</pre>
                </>
              ) : (
                <Spinner label="正在检查驱动和离线模型" />
              )}
              <p className="dialog-description">
                关闭浏览器后托盘服务继续运行；退出工作台会停止引擎并保存任务状态。
              </p>
            </DialogContent>
            <DialogActions>
              <Button onClick={() => setDiagnosticOpen(false)}>关闭</Button>
              <Button
                icon={<Power size={15} />}
                onClick={() =>
                  void action(async () => {
                    await editor.flush();
                    await api("/shutdown", "POST", {});
                    notify("工作台已退出，未完成任务下次可继续");
                  })
                }
              >
                退出工作台
              </Button>
            </DialogActions>
          </DialogBody>
        </DialogSurface>
      </Dialog>
    </div>
  );
}
