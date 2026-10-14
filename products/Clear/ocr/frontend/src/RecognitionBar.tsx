import { useId } from "react";
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
  const recommendedEngine = modes.find((item) => item.id === mode)?.engine;
  const engineLabel = engine === "all"
    ? "四引擎顺序对比"
    : engines[engine]?.name || engine;
  const settingsSummary = [
    mode === "compare" ? "模型对比" : "",
    engine !== recommendedEngine ? `引擎：${engineLabel}` : "",
    preprocess !== "none" ? preprocessLabel : "",
  ].filter(Boolean).join(" · ");
  const summaryId = useId();
  const selectMode = (item: (typeof modes)[number]) => {
    setMode(item.id);
    setEngine(item.engine);
    chooseTab(item.id === "compare" ? "compare" : item.id === "table" ? "table" : "text");
  };

  return (
    <section className="recognition-bar ocr-recognition" aria-label="识别设置">
      <div
        className="mode-buttons ocr-recognition__modes"
        role="group"
        aria-label="识别模式"
      >
        {modes.filter((m) => m.id !== "compare").map((m) => (
          <button
            key={m.id}
            type="button"
            className={mode === m.id ? "active" : ""}
            title={m.description}
            aria-pressed={mode === m.id}
            onClick={() => selectMode(m)}
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
        <Popover positioning="below-end">
          <PopoverTrigger disableButtonEnhancement>
            <Button
              type="button"
              appearance="subtle"
              className="ocr-recognition__settings"
              icon={<SlidersHorizontal size={15} aria-hidden="true" />}
              aria-label="识别设置"
              aria-describedby={settingsSummary ? summaryId : undefined}
              title={settingsSummary || "使用当前模式的推荐引擎，保留当前图像"}
            >
              <span>识别设置</span>
              <ChevronDown size={12} aria-hidden="true" />
            </Button>
          </PopoverTrigger>
          <PopoverSurface
            className="ocr-recognition-popover"
            aria-label="识别高级设置"
          >
            <div className="ocr-recognition-popover__heading">识别设置</div>
            <p>调整本次识别的引擎与图像处理方式。</p>
            <label className="ocr-recognition-popover__field">
              <span>识别引擎</span>
              <select
                aria-label="识别引擎"
                value={engine}
                onChange={(e) => setEngine(e.target.value)}
              >
                {Object.keys(engines).map((key) => (
                  <option key={key} value={key}>{engines[key].name}</option>
                ))}
                <option value="all">四引擎顺序对比</option>
              </select>
            </label>
            <label className="ocr-recognition-popover__field">
              <span>批次预处理</span>
              <select
                aria-label="批次预处理"
                value={preprocess}
                onChange={(e) => setPreprocess(e.target.value)}
              >
                {preprocessOptions.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
            <div className="ocr-recognition-popover__comparison">
              <Button
                type="button"
                appearance={mode === "compare" ? "primary" : "secondary"}
                icon={<Columns3 size={16} aria-hidden="true" />}
                aria-pressed={mode === "compare"}
                onClick={() => selectMode(modes.find((item) => item.id === "compare")!)}
              >
                模型对比
              </Button>
              <p>顺序运行四种引擎，分别查看和采用识别结果。</p>
            </div>
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
        {settingsSummary && <span id={summaryId} className="ocr-recognition__summary">{settingsSummary}</span>}
      </div>
    </section>
  );
}
