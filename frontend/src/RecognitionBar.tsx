import { Button } from "@fluentui/react-components";
import { ScanLine, PenLine, Table2, Columns3, Play } from "lucide-react";
import type { Engine } from "./types";
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
  imageCount: number;
  selectedCount: number;
  hasActive: boolean;
  onRun: () => void;
}) {
  return (
    <section className="recognition-bar">
      <div className="mode-buttons">
        {modes.map((m) => (
          <button
            key={m.id}
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
            <m.icon size={17} />
            {m.name}
          </button>
        ))}
      </div>
      <div className="engine-choice">
        <span className="recognition-scope">
          {selectedCount
            ? `已选 ${selectedCount} 张`
            : hasActive
              ? "当前图片 · 1 张"
              : "等待导入资料"}
        </span>
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
          disabled={busy || !imageCount}
          onClick={onRun}
        >
          {busy ? "处理中" : "开始识别"}
        </Button>
      </div>
    </section>
  );
}
