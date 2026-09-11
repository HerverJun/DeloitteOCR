import {
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type CSSProperties,
} from "react";
import { Button } from "@fluentui/react-components";
import {
  PanelLeftClose,
  PanelLeftOpen,
  Maximize2,
  Minimize2,
  Image,
  Table2,
} from "lucide-react";
import { usePreference } from "./workspacePreferences";

export function WorkspaceLayout({
  projectName,
  sidebar,
  toolbar,
  image,
  children,
  queue,
}: {
  projectName: string;
  sidebar: ReactNode;
  toolbar: ReactNode;
  image: ReactNode;
  children: ReactNode;
  queue: ReactNode;
}) {
  const [width, setWidth] = useState(window.innerWidth);
  const [height, setHeight] = useState(window.innerHeight);
  const [sidebarOpen, setSidebarOpen] = usePreference(
    "ocr-ui-sidebar",
    window.innerWidth >= 1200,
    (v): v is boolean => typeof v === "boolean",
  );
  const [ratio, setRatio] = usePreference(
    "ocr-ui-split",
    48,
    (v): v is number =>
      typeof v === "number" && Number.isFinite(v) && v >= 30 && v <= 65,
  );
  const [expanded, setExpanded] = useState(false);
  const [mobilePane, setMobilePane] = useState("result");
  const grid = useRef<HTMLDivElement>(null);
  const narrow = width < 1000 || height < 680;
  useEffect(() => {
    let previousWidth = window.innerWidth;
    const resize = () => {
      if (previousWidth >= 1200 && window.innerWidth < 1200)
        setSidebarOpen(false);
      previousWidth = window.innerWidth;
      setWidth(window.innerWidth);
      setHeight(window.innerHeight);
    };
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, []);
  const bound = (value: number) => Math.max(30, Math.min(65, value));
  return (
    <div
      className={`workspace-grid ${sidebarOpen ? "sidebar-open" : "sidebar-closed"}`}
    >
      <aside className="sidebar" aria-label="项目资料" hidden={!sidebarOpen}>
        {sidebar}
      </aside>
      <main id="workspace" className="main-workspace">
        <div className="workspace-heading">
          <div className="workspace-heading-left">
            <Button
              appearance="subtle"
              icon={
                sidebarOpen ? (
                  <PanelLeftClose size={18} />
                ) : (
                  <PanelLeftOpen size={18} />
                )
              }
              aria-label={sidebarOpen ? "收起资料栏" : "展开资料栏"}
              aria-expanded={sidebarOpen}
              onClick={() => setSidebarOpen(!sidebarOpen)}
            />
            <div className="workspace-title">
              <span className="workspace-breadcrumb">
                工作空间 <span>/</span> {projectName || "新建项目"}
              </span>
              <h1>文档识别与校对</h1>
            </div>
          </div>
          <div className="view-actions">
            {narrow ? (
              <>
                <Button
                  appearance={mobilePane === "image" ? "primary" : "subtle"}
                  icon={<Image size={16} />}
                  onClick={() => setMobilePane("image")}
                >
                  原图
                </Button>
                <Button
                  appearance={mobilePane === "result" ? "primary" : "subtle"}
                  icon={<Table2 size={16} />}
                  onClick={() => setMobilePane("result")}
                >
                  校对
                </Button>
              </>
            ) : (
              <Button
                appearance="subtle"
                icon={
                  expanded ? <Minimize2 size={16} /> : <Maximize2 size={16} />
                }
                onClick={() => setExpanded(!expanded)}
              >
                {expanded ? "恢复双栏" : "展开校对区"}
              </Button>
            )}
          </div>
        </div>
        {toolbar}
        <div
          ref={grid}
          className={`document-grid ${expanded && !narrow ? "result-expanded" : ""} ${narrow ? "single-pane pane-" + mobilePane : ""}`}
          style={
            {
              "--image-ratio": `${ratio}fr`,
              "--result-ratio": `${100 - ratio}fr`,
            } as CSSProperties
          }
        >
          {image}
          <div
            className="panel-separator"
            role="separator"
            aria-label="调整原图与校对区宽度"
            aria-orientation="vertical"
            aria-valuemin={30}
            aria-valuemax={65}
            aria-valuenow={Math.round(ratio)}
            tabIndex={0}
            onDoubleClick={() => setRatio(48)}
            onKeyDown={(e) => {
              if (
                ["ArrowLeft", "ArrowRight", "Home", "End", "Enter"].includes(
                  e.key,
                )
              ) {
                e.preventDefault();
                setRatio(
                  e.key === "Home"
                    ? 30
                    : e.key === "End"
                      ? 65
                      : e.key === "Enter"
                        ? 48
                        : bound(ratio + (e.key === "ArrowLeft" ? -2 : 2)),
                );
              }
            }}
            onPointerDown={(e) => {
              e.currentTarget.setPointerCapture(e.pointerId);
            }}
            onPointerMove={(e) => {
              if (
                !e.currentTarget.hasPointerCapture(e.pointerId) ||
                !grid.current
              )
                return;
              const r = grid.current.getBoundingClientRect();
              setRatio(bound(((e.clientX - r.left) / r.width) * 100));
            }}
            onPointerUp={(e) => {
              if (e.currentTarget.hasPointerCapture(e.pointerId))
                e.currentTarget.releasePointerCapture(e.pointerId);
            }}
          >
            <span />
          </div>
          {children}
        </div>
        {queue}
      </main>
    </div>
  );
}
