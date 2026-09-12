import {
  Button,
  Popover,
  PopoverSurface,
  PopoverTrigger,
} from "@fluentui/react-components";
import {
  ScanLine,
  PenLine,
  Table2,
  Columns3,
  Play,
  SlidersHorizontal,
  ChevronDown,
} from "lucide-react";
import type { Engine } from "./types";
import "./recognition.css";

const preprocessOptions = [
  { value: "none", label: "保留当前图像" },
  { value: "contrast", label: "增强对比度" },
  { value: "rotate", label: "顺时针旋转 90°" },
  { value: "rotate-contrast", label: "旋转并增强对比度" },
];

export const modes = [
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
export function RecognitionBar({
  mode,
  setMode,
  engine,
  setEngine,
  preprocess,
  setPreprocess,
  chooseTab,
  engines,
  busy,
  recognitionDisabled = false,
  imageCount,
  selectedCount,
  hasActive,
  onRun,
}: {
  mode: string;
  setMode: (v: string) => void;
  engine: string;
  setEngine: (v: string) => void;
  preprocess: string;
  setPreprocess: (v: string) => void;
  chooseTab: (v: string) => void;
  engines: Record<string, Engine>;
  busy: boolean;
  recognitionDisabled?: boolean;
  imageCount: number;
  selectedCount: number;
  hasActive: boolean;
  onRun: () => void;
}) {
  const preprocessLabel =
    preprocessOptions.find((option) => option.value === preprocess)?.label ??
    "保留当前图像";

  return (
    <section className="recognition-bar ocr-recognition" aria-label="识别设置">
      <div
        className="mode-buttons ocr-recognition__modes"
        role="group"
        aria-label="识别模式"
      >
        {modes.map((m) => (
          <button
            key={m.id}
            type="button"
            className={mode === m.id ? "active" : ""}
            title={m.description}
            aria-pressed={mode === m.id}
            onClick={() => {
              setMode(m.id);
              setEngine(m.engine);
              chooseTab(
                m.id === "compare"
                  ? "compare"
                  : m.id === "table"
                    ? "table"
                    : "text",
              );
            }}
          >
            <m.icon size={16} aria-hidden="true" />
            {m.name}
          </button>
        ))}
      </div>
      <div className="engine-choice ocr-recognition__actions">
        <span className="recognition-scope ocr-recognition__scope">
          {selectedCount
            ? `已选 ${selectedCount} 张`
            : hasActive
              ? "当前图片 · 1 张"
              : "等待导入资料"}
        </span>
        <label className="ocr-recognition__engine">
          <span>引擎</span>
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
        <Popover positioning="below-end">
          <PopoverTrigger disableButtonEnhancement>
            <Button
              type="button"
              appearance="subtle"
              className="ocr-recognition__preprocess"
              icon={<SlidersHorizontal size={15} aria-hidden="true" />}
              aria-label={`批次预处理设置，当前：${preprocessLabel}`}
              title={`批次预处理：${preprocessLabel}`}
            >
              <span>{preprocessLabel}</span>
              <ChevronDown size={12} aria-hidden="true" />
            </Button>
          </PopoverTrigger>
          <PopoverSurface
            className="ocr-recognition-popover"
            aria-label="批次预处理设置"
          >
            <div className="ocr-recognition-popover__heading">批次预处理</div>
            <p>选择本次识别使用的图像处理方式。</p>
            <select
              aria-label="批次预处理"
              value={preprocess}
              onChange={(e) => setPreprocess(e.target.value)}
            >
              {preprocessOptions.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </PopoverSurface>
        </Popover>
        <Button
          type="button"
          appearance="primary"
          className="ocr-recognition__run"
          icon={<Play size={15} fill="currentColor" />}
          disabled={busy || recognitionDisabled || !imageCount}
          onClick={onRun}
        >
          {busy ? "处理中" : "开始识别"}
        </Button>
      </div>
    </section>
  );
}
