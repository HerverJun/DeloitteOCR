import { useCallback, useEffect, useRef, useState } from "react";
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
  Play,
  Pause,
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
  RefreshCw,
  ArrowRight,
  Plus,
  Files,
  Power,
} from "lucide-react";
import { api, request, download } from "./api";
import { ImageCanvas } from "./ImageCanvas";
import { TableEditor } from "./TableEditor";
import { useEditor } from "./useEditor";
import { ProjectStorage } from "./ProjectStorage";
import { EnginePackages } from "./EnginePackages";
import { engineNames, statuses } from "./types";
import type {
  Project,
  ProjectState,
  Result,
  Photo,
  Engine,
  Task,
  Edit,
} from "./types";

const modes = [
  {
    id: "text",
    name: "照片文字",
    description: "快速提取文字与坐标",
    engine: "ppocr",
    icon: ScanLine,
  },
  {
    id: "handwriting",
    name: "手写",
    description: "手写内容的识别候选",
    engine: "hunyuan",
    icon: PenLine,
  },
  {
    id: "table",
    name: "表格",
    description: "解析行列与合并关系",
    engine: "paddlevl",
    icon: Table2,
  },
  {
    id: "compare",
    name: "模型对比",
    description: "四种结果，独立校对",
    engine: "all",
    icon: Columns3,
  },
];
export function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [project, setProject] = useState<ProjectState | null>(null);
  const selectedProject = useRef("");
  const [engines, setEngines] = useState<Record<string, Engine>>({});
  const [active, setActive] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [mode, setMode] = useState("text");
  const [engine, setEngine] = useState("ppocr");
  const [preprocess, setPreprocess] = useState("none");
  const [tab, setTab] = useState("text");
  const [busy, setBusy] = useState(false);
  const [initial, setInitial] = useState(true);
  const [message, setMessage] = useState("");
  const [error, setError] = useState(false);
  const [showQueue, setShowQueue] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [highlight, setHighlight] = useState<number | null>(null);
  const [newProject, setNewProject] = useState(false);
  const [projectName, setProjectName] = useState("");
  const [rename, setRename] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [exportFormat, setExportFormat] = useState("xlsx");
  const [exportMany, setExportMany] = useState(false);
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
    setMessage(text.replace(/^Error: /, ""));
    setError(isError);
    if (toastTimer.current) clearTimeout(toastTimer.current);
    if (!isError) toastTimer.current = setTimeout(() => setMessage(""), 5000);
  }, []);
  const onError = useCallback((text: string) => notify(text, true), [notify]);
  const editor = useEditor(onError);
  const refresh = useCallback(async (id: string) => {
    if (!id) return;
    const value = await api<ProjectState>("/projects/" + id);
    if (selectedProject.current === id) setProject(value);
    return value;
  }, []);
  const chooseProject = useCallback(
    async (id: string) => {
      await editor.flush();
      selectedProject.current = id;
      setProjectId(id);
      setProject(null);
      setActive("");
      setSelected([]);
      await editor.load(null);
      localStorage.setItem("ocr-project", id);
      await refresh(id);
    },
    [editor.flush, editor.load, refresh],
  );
  useEffect(() => {
    api("/state")
      .then((value) => {
        setProjects(value.projects);
        setEngines(value.engines);
        const saved = localStorage.getItem("ocr-project");
        const id =
          value.projects.find((p: Project) => p.id === saved)?.id ||
          value.projects[0]?.id;
        if (id) void chooseProject(id).catch(onError);
      })
      .catch(onError)
      .finally(() => setInitial(false));
  }, []);
  useEffect(() => {
    if (!projectId) return;
    const interval = setInterval(() => {
      void refresh(projectId).catch(onError);
    }, 1200);
    return () => clearInterval(interval);
  }, [projectId, refresh, onError]);
  const photo = project?.images.find((p) => p.id === active) || null;
  const versions = project?.versions.filter((v) => v.image_id === active) || [];
  const version = versions.find((v) => v.id === photo?.active_version) || null;
  const tasks = project?.tasks.filter((t) => t.image_id === active) || [];
  const finished = tasks.filter((t) => t.status === "succeeded" && t.result_id);
  const currentResult =
    photo?.selected_result || finished[finished.length - 1]?.result_id || null;
  const loadedResultId = editor.result?.id;
  useEffect(() => {
    if (!active) return;
    void editor.load(currentResult).catch(onError);
  }, [active, currentResult]);
  useEffect(() => {
    if (project && !active && project.images.length)
      setActive(project.images[0].id);
  }, [project, active]);
  useEffect(() => {
    setHighlight(null);
  }, [active, loadedResultId]);
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
    setBusy(true);
    try {
      return await fn();
    } catch (e) {
      onError(String(e));
    } finally {
      setBusy(false);
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
      const body = new FormData();
      files.forEach((file) => body.append("files", file, file.name));
      const response = await request("/projects/" + id + "/images", {
        method: "POST",
        body,
      });
      const value = await response.json();
      await refresh(id);
      if (value.images.length) {
        setActive(value.images[0].id);
        setSelected(value.images.map((p: Photo) => p.id));
      }
      notify(
        `导入 ${value.images.length} 张图片` +
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
  const selectResult = async (id: string) => {
    await editor.flush();
    await api("/images/" + active + "/selection", "PUT", { result_id: id });
    await refresh(projectId);
    await editor.load(id);
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
      const photos =
        latest.images.filter(
          (p) => !selected.length || selected.includes(p.id),
        ) || [];
      ids = photos
        .map(
          (p) =>
            p.selected_result ||
            latest.tasks
              .filter((t) => t.image_id === p.id && t.result_id)
              .at(-1)?.result_id,
        )
        .filter(Boolean) as string[];
      if (ids.length !== photos.length)
        throw new Error(
          `所选 ${photos.length} 张图片中有 ${photos.length - ids.length} 张尚无可导出的结果，请等待识别或调整选择。`,
        );
    } else if (editor.result) ids = [editor.result.id];
    await download(
      ids,
      exportFormat,
      exportMany && exportFormat === "xlsx" && exportAggregate,
    );
    setExportOpen(false);
    notify("导出文件已生成");
  };
  const findNext = () => {
    if (!editor.edit || !search) return;
    let i = editor.edit.text.indexOf(search, searchIndex);
    if (i < 0) i = editor.edit.text.indexOf(search);
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
      <header className="app-header">
        <a className="brand" href="#" onClick={(e) => e.preventDefault()}>
          <span className="brand-mark">
            <FileText size={22} />
          </span>
          <strong>纸页</strong>
          <span>离线 OCR 工作台</span>
        </a>
        <div className="header-right">
          <EnginePackages
            onError={onError}
            onChange={async () => {
              const value = await api("/state");
              setEngines(value.engines);
              notify("引擎版本已更新，已有任务仍使用原版本");
            }}
          />
          <span className="offline-status">
            <span />
            仅在本机处理
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
        </div>
      </header>
      <div className="workspace-grid">
        <aside className="sidebar">
          <div className="project-control">
            <span className="section-label">工作项目</span>
            <Button
              aria-label="新建项目"
              appearance="subtle"
              size="small"
              icon={<FolderPlus size={17} />}
              onClick={() => {
                setRename(false);
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
              onChange={(e) => void action(() => chooseProject(e.target.value))}
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
            <button
              title="重命名项目"
              disabled={!projectId}
              onClick={() => {
                setRename(true);
                setProjectName(project?.project.name || "");
                setNewProject(true);
              }}
            >
              <PenLine size={14} />
            </button>
          </div>
          <ProjectStorage
            id={projectId}
            name={project?.project.name || ""}
            onError={onError}
            onDelete={async (confirmation) => {
              await editor.flush();
              const value = await api("/projects/" + projectId, "DELETE", {
                confirmation,
              });
              const remaining = projects.filter((p) => p.id !== projectId);
              setProjects(remaining);
              await chooseProject(remaining[0]?.id || "");
              notify(
                value.cleanup_pending
                  ? "项目已删除，部分文件将在下次启动继续清理"
                  : "项目已清理",
              );
            }}
          />
          <div className="import-actions">
            <Button
              appearance="primary"
              icon={<Upload size={16} />}
              disabled={busy}
              onClick={() => fileInput.current?.click()}
            >
              导入图片
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
            accept=".jpg,.jpeg,.png,.bmp,.tif,.tiff,.webp,.heic,.heif"
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
          <div className="photo-list-head">
            <label>
              <input
                type="checkbox"
                aria-label="选择全部图片"
                checked={
                  !!project?.images.length &&
                  selected.length === project.images.length
                }
                onChange={(e) =>
                  setSelected(
                    e.target.checked
                      ? project?.images.map((p) => p.id) || []
                      : [],
                  )
                }
              />
              图片 <span>{project?.images.length || 0}</span>
            </label>
            <span>{selected.length ? `已选 ${selected.length}` : ""}</span>
          </div>
          <div className="photo-list">
            {project?.images.map((p, index) => {
              const latest = project.tasks
                .filter((t) => t.image_id === p.id)
                .at(-1);
              return (
                <div
                  key={p.id}
                  className={"photo-item " + (p.id === active ? "active" : "")}
                  onClick={() => void action(() => selectPhoto(p))}
                >
                  <input
                    aria-label={`选择 ${p.name}`}
                    type="checkbox"
                    checked={selected.includes(p.id)}
                    onClick={(e) => e.stopPropagation()}
                    onChange={(e) =>
                      setSelected((old) =>
                        e.target.checked
                          ? [...old, p.id]
                          : old.filter((id) => id !== p.id),
                      )
                    }
                  />
                  <Thumbnail id={p.active_version} />
                  <div className="photo-name">
                    <strong title={p.name}>{p.name}</strong>
                    <small>
                      {latest ? statuses[latest.status] : "尚未识别"}
                    </small>
                  </div>
                  <span className="photo-number">
                    {String(index + 1).padStart(2, "0")}
                  </span>
                </div>
              );
            })}
            {!project?.images.length && (
              <div className="sidebar-empty">
                <Files size={30} />
                <p>把照片或文件夹拖到这里</p>
                <small>原始文件会保存在项目中</small>
              </div>
            )}
          </div>
          <button
            className={"queue-summary " + (showQueue ? "open" : "")}
            onClick={() => setShowQueue(!showQueue)}
          >
            <LayoutList size={18} />
            <span>
              任务队列
              <small>
                {pending
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
        </aside>
        <main className="main-workspace">
          <section className="recognition-bar">
            <div className="mode-buttons">
              {modes.map((m) => (
                <button
                  key={m.id}
                  className={mode === m.id ? "active" : ""}
                  title={m.description}
                  onClick={() => {
                    setMode(m.id);
                    setEngine(m.engine);
                    setTab(
                      m.id === "compare"
                        ? "compare"
                        : m.id === "table"
                          ? "table"
                          : "text",
                    );
                  }}
                >
                  <m.icon size={17} />
                  {m.name}
                </button>
              ))}
            </div>
            <div className="engine-choice">
              <label>
                批次预处理
                <select
                  aria-label="批次预处理"
                  value={preprocess}
                  onChange={(e) => setPreprocess(e.target.value)}
                >
                  <option value="none">保留当前图像</option>
                  <option value="contrast">增强对比度</option>
                  <option value="rotate">顺时针旋转 90°</option>
                  <option value="rotate-contrast">旋转并增强对比度</option>
                </select>
              </label>
              <label>
                识别引擎
                <select
                  aria-label="识别引擎"
                  value={engine}
                  onChange={(e) => setEngine(e.target.value)}
                >
                  {Object.keys(engines).map((key) => (
                    <option key={key} value={key}>
                      {engines[key].name}
                    </option>
                  ))}
                  <option value="all">四引擎顺序对比</option>
                </select>
              </label>
              <Button
                appearance="primary"
                icon={<Play size={15} fill="currentColor" />}
                disabled={busy || !project?.images.length}
                onClick={() => void action(() => run())}
              >
                {busy ? "处理中" : "开始识别"}
                {selected.length > 1 ? ` (${selected.length})` : ""}
              </Button>
            </div>
          </section>
          <div
            className={
              "document-grid " + (tab === "compare" ? "comparison-layout" : "")
            }
          >
            <ImageCanvas
              version={version}
              versions={versions}
              blocks={blocks}
              highlight={highlight}
              onVersion={(id) => void changeVersion(id)}
              onTransform={(op) => void transform(op)}
              onRegion={(box) => void region(box)}
              busy={busy}
            />
            <section className="result-workspace" aria-label="识别与校对结果">
              <div className="result-title">
                <div>
                  <span className="section-label">识别与校对</span>
                  <h2>{photo ? photo.name : "等待图片"}</h2>
                </div>
                <span
                  className={
                    "save-status " +
                    (editor.saveState === "保存失败" ? "failed" : "")
                  }
                >
                  <CheckCircle2 size={14} />
                  {editor.saveState}
                </span>
              </div>
              {finished.length > 0 && (
                <div className="result-selector">
                  <select
                    aria-label="当前识别结果"
                    value={editor.result?.id || ""}
                    onChange={(e) =>
                      void action(() => selectResult(e.target.value))
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
                    <span>
                      {editor.result.original.elapsed_seconds.toFixed(2)} 秒
                    </span>
                  )}
                </div>
              )}
              <div className="result-tabs">
                <button
                  className={tab === "text" ? "active" : ""}
                  onClick={() => setTab("text")}
                >
                  <FileText size={15} />
                  文字
                </button>
                <button
                  className={tab === "table" ? "active" : ""}
                  onClick={() => setTab("table")}
                >
                  <Table2 size={15} />
                  表格
                  {editor.edit?.tables.length
                    ? ` ${editor.edit.tables.length}`
                    : ""}
                </button>
                <button
                  className={tab === "compare" ? "active" : ""}
                  onClick={() => setTab("compare")}
                >
                  <Columns3 size={15} />
                  对比
                </button>
              </div>
              {!editor.result || !editor.edit ? (
                <div className="empty-panel">
                  <ScanLine size={34} />
                  <h3>
                    {tasks.some((t) => ["running", "queued"].includes(t.status))
                      ? "正在识别图片"
                      : "识别结果将在这里显示"}
                  </h3>
                  <p>
                    {tasks.some((t) => ["running", "queued"].includes(t.status))
                      ? "每个模型独立处理，完成后可校对文字和表格。"
                      : "选择识别方式后点击「开始识别」，原始输出和校对记录都会保留。"}
                  </p>
                  {tasks.some((t) => t.status === "running") && (
                    <Spinner size="small" label="模型处理中" />
                  )}
                </div>
              ) : (
                <>
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
                      onClick={() =>
                        void action(async () => {
                          await navigator.clipboard.writeText(
                            editor.edit!.text,
                          );
                          notify("文字已复制");
                        })
                      }
                    >
                      复制
                    </Button>
                    <span className="edit-note">原始识别独立保留</span>
                  </div>
                  {editor.saveState === "保存失败" && (
                    <div className="inline-warning">
                      编辑尚未保存。
                      <Button
                        size="small"
                        onClick={() => void action(editor.flush)}
                      >
                        重试保存
                      </Button>
                      <Button
                        size="small"
                        onClick={() => void action(editor.reload)}
                      >
                        重新加载
                      </Button>
                    </div>
                  )}
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
                        spellCheck={false}
                        value={editor.edit.text}
                        onChange={(e) =>
                          editor.change({
                            ...editor.edit!,
                            text: e.target.value,
                          })
                        }
                      />
                      <details className="text-blocks">
                        <summary>
                          定位文字区域 ({editor.result.original.blocks.length})
                        </summary>
                        {editor.result.original.blocks.map((block, i) => (
                          <button
                            key={i}
                            disabled={!block.polygon}
                            className={highlight === i ? "active" : ""}
                            onClick={() => {
                              if (!matchesVersion)
                                void changeVersion(
                                  editor.result!.original
                                    .project_image_version!,
                                );
                              setHighlight(i);
                            }}
                          >
                            <span>{i + 1}</span>
                            {block.text.slice(0, 90)}
                            {block.confidence !== null && (
                              <small>
                                {Math.round(block.confidence * 100)}%
                              </small>
                            )}
                          </button>
                        ))}
                      </details>
                      <div className="text-count">
                        {editor.edit.text.length} 字符
                        <span>UTF-8 · 保留原始编号</span>
                      </div>
                    </div>
                  )}
                  {tab === "table" && (
                    <TableEditor
                      tables={editor.edit.tables}
                      onError={onError}
                      onChange={(tables) =>
                        editor.change({ ...editor.edit!, tables })
                      }
                    />
                  )}
                  {tab === "compare" && (
                    <div className="comparison-panel">
                      <p>
                        选择采用的结果。各模型原文、表格和校对历史分别保存。
                      </p>
                      {compared.length < 2 && (
                        <div className="inline-warning">
                          当前仅有一种结果。选择「四引擎顺序对比」后开始识别。
                        </div>
                      )}
                      {compared.map((r, i) => (
                        <article className="comparison-result" key={r.id}>
                          <header>
                            <strong>{engineNames[r.original.engine]}</strong>
                            <span>
                              {r.original.elapsed_seconds.toFixed(2)} 秒
                            </span>
                            <Button
                              size="small"
                              appearance={
                                r.id === photo?.selected_result
                                  ? "primary"
                                  : "secondary"
                              }
                              onClick={() =>
                                void action(() => selectResult(r.id))
                              }
                            >
                              {r.id === photo?.selected_result
                                ? "已采用"
                                : "采用结果"}
                            </Button>
                          </header>
                          <pre>{r.edited.text}</pre>
                          {i > 0 && (
                            <div className="difference-note">
                              与首个结果比较：
                              {r.edited.text === compared[0].edited.text
                                ? "文字完全一致"
                                : `文字不同，字符数相差 ${r.edited.text.length - compared[0].edited.text.length}`}
                              <details>
                                <summary>查看差异行</summary>
                                {lineDifferences(
                                  compared[0].edited.text,
                                  r.edited.text,
                                ).map((line, j) => (
                                  <div className={line.kind} key={j}>
                                    {line.kind === "removed" ? "− " : "+ "}
                                    {line.text}
                                  </div>
                                ))}
                              </details>
                            </div>
                          )}
                        </article>
                      ))}
                    </div>
                  )}
                </>
              )}
              <footer className="result-footer">
                <span>
                  {editor.edit?.tables.length
                    ? `${editor.edit.tables.length} 个表格可导出`
                    : "可导出文字与原始 JSON"}
                </span>
                <Button
                  appearance="primary"
                  icon={<Download size={16} />}
                  disabled={!editor.result || busy}
                  onClick={() => {
                    setExportFormat(
                      editor.edit?.tables.length ? "xlsx" : "txt",
                    );
                    setExportOpen(true);
                  }}
                >
                  导出结果
                </Button>
              </footer>
            </section>
          </div>
          {showQueue && (
            <section className="task-drawer" aria-label="任务队列">
              <header>
                <h3>
                  任务队列 <span>{project?.tasks.length || 0}</span>
                </h3>
                <div>
                  <Button
                    size="small"
                    icon={<Pause size={14} />}
                    onClick={() => void action(() => queueAction("pause"))}
                  >
                    暂停等待项
                  </Button>
                  <Button
                    size="small"
                    icon={<Play size={14} />}
                    onClick={() => void action(() => queueAction("resume"))}
                  >
                    继续
                  </Button>
                  <Button
                    size="small"
                    icon={<RefreshCw size={14} />}
                    onClick={() => void action(() => queueAction("retry"))}
                  >
                    重试失败项
                  </Button>
                  <Button
                    size="small"
                    appearance="subtle"
                    aria-label="收起任务队列"
                    icon={<X size={16} />}
                    onClick={() => setShowQueue(false)}
                  />
                </div>
              </header>
              <div className="task-rows">
                {project?.tasks
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
                      <span
                        className="task-filename"
                        title={t.error || undefined}
                      >
                        {project.images.find((p) => p.id === t.image_id)?.name}
                        <small>{t.error || t.phase}</small>
                      </span>
                      <span>{engineNames[t.engine]}</span>
                      <div>
                        {t.result_id ? (
                          <Button
                            size="small"
                            onClick={() =>
                              void action(async () => {
                                await editor.flush();
                                setActive(t.image_id);
                                await api(
                                  "/images/" + t.image_id + "/selection",
                                  "PUT",
                                  { result_id: t.result_id },
                                );
                                await refresh(projectId);
                                await editor.load(t.result_id);
                              })
                            }
                          >
                            查看
                          </Button>
                        ) : t.result_version_id ? (
                          <Button
                            size="small"
                            onClick={() =>
                              void action(async () => {
                                await editor.flush();
                                setActive(t.image_id);
                                await api(
                                  "/images/" + t.image_id + "/version",
                                  "PUT",
                                  { version_id: t.result_version_id },
                                );
                                await refresh(projectId);
                              })
                            }
                          >
                            查看图片
                          </Button>
                        ) : ["failed", "cancelled"].includes(t.status) ? (
                          <Button
                            size="small"
                            onClick={() =>
                              void action(() => queueAction("retry", t))
                            }
                          >
                            重试
                          </Button>
                        ) : ["paused", "interrupted"].includes(t.status) ? (
                          <Button
                            size="small"
                            onClick={() =>
                              void action(() => queueAction("resume", t))
                            }
                          >
                            继续
                          </Button>
                        ) : (
                          <Button
                            size="small"
                            onClick={() =>
                              void action(() => queueAction("cancel", t))
                            }
                          >
                            取消
                          </Button>
                        )}
                      </div>
                    </div>
                  ))}
                {!project?.tasks.length && (
                  <p className="queue-empty">尚无任务。选择图片后开始识别。</p>
                )}
              </div>
            </section>
          )}
        </main>
      </div>
      {message && (
        <div role="alert" className={"toast " + (error ? "error" : "")}>
          <span>
            {error ? <AlertCircle size={18} /> : <CheckCircle2 size={18} />}
          </span>
          <p>{message}</p>
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
                </select>
              </label>
              <label className="export-scope">
                <input
                  type="checkbox"
                  checked={exportMany}
                  onChange={(e) => setExportMany(e.target.checked)}
                />
                导出选中图片采用的结果
                {selected.length ? ` (${selected.length} 张)` : " (当前项目)"}
              </label>
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
            </DialogContent>
            <DialogActions>
              <Button onClick={() => setExportOpen(false)}>取消</Button>
              <Button
                appearance="primary"
                disabled={busy}
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
        open={diagnosticOpen}
        onOpenChange={(_, data) => setDiagnosticOpen(data.open)}
      >
        <DialogSurface>
          <DialogBody>
            <DialogTitle>运行环境</DialogTitle>
            <DialogContent>
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

function Thumbnail({ id }: { id: string }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    let alive = true;
    let value = "";
    request("/versions/" + id + "/thumbnail")
      .then((r) => r.blob())
      .then((blob) => {
        value = URL.createObjectURL(blob);
        if (alive) setUrl(value);
        else URL.revokeObjectURL(value);
      })
      .catch(() => {});
    return () => {
      alive = false;
      if (value) URL.revokeObjectURL(value);
    };
  }, [id]);
  return (
    <div className="thumbnail">
      {url ? <img src={url} alt="" /> : <FileText size={20} />}
    </div>
  );
}
function lineDifferences(left: string, right: string) {
  const a = left.split("\n"),
    b = right.split("\n");
  const differences: { kind: string; text: string }[] = [];
  const count = Math.max(a.length, b.length);
  for (let i = 0; i < count; i++)
    if (a[i] !== b[i]) {
      if (a[i] !== undefined) differences.push({ kind: "removed", text: a[i] });
      if (b[i] !== undefined) differences.push({ kind: "added", text: b[i] });
    }
  return differences;
}
