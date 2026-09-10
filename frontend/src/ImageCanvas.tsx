import { useEffect, useRef, useState } from "react";
import { Button } from "@fluentui/react-components";
import {
  ZoomIn,
  ZoomOut,
  Scan,
  Hand,
  Crop,
  RotateCw,
  Focus,
  CornerUpLeft,
  Contrast,
  Image as ImageIcon,
} from "lucide-react";
import type { Version, Block } from "./types";
import { request } from "./api";

type Point = [number, number];
export function ImageCanvas({
  version,
  versions,
  blocks,
  highlight,
  onVersion,
  onTransform,
  onRegion,
  busy,
}: {
  version: Version | null;
  versions: Version[];
  blocks: Block[];
  highlight: number | null;
  onVersion: (id: string) => void;
  onTransform: (op: unknown) => void;
  onRegion: (box: number[]) => void;
  busy: boolean;
}) {
  const [url, setUrl] = useState("");
  const [loadError, setLoadError] = useState("");
  const [zoom, setZoom] = useState(1);
  const [fit, setFit] = useState(true);
  const [mode, setMode] = useState<"pan" | "crop" | "perspective" | "region">(
    "pan",
  );
  const [box, setBox] = useState<number[] | null>(null);
  const [points, setPoints] = useState<Point[]>([]);
  const stage = useRef<HTMLDivElement>(null);
  const image = useRef<HTMLDivElement>(null);
  const drag = useRef<{
    point: Point;
    scroll: Point;
    type: string;
    corner?: number;
  } | null>(null);
  const [size, setSize] = useState([800, 700]);
  useEffect(() => {
    if (!stage.current) return;
    const observer = new ResizeObserver((entries) => {
      const r = entries[0].contentRect;
      setSize([r.width, r.height]);
    });
    observer.observe(stage.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    let active = true;
    let objectUrl = "";
    setUrl("");
    setLoadError("");
    setBox(null);
    setPoints([]);
    setFit(true);
    setMode("pan");
    if (version)
      request("/versions/" + version.id + "/image")
        .then((r) => r.blob())
        .then((blob) => {
          objectUrl = URL.createObjectURL(blob);
          if (active) setUrl(objectUrl);
          else URL.revokeObjectURL(objectUrl);
        })
        .catch((e) => {
          if (active) setLoadError(String(e));
        });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [version?.id]);
  const scale = version
    ? fit
      ? Math.min(
          (size[0] - 64) / version.width,
          (size[1] - 64) / version.height,
          1,
        )
      : zoom
    : 1;
  const point = (event: React.PointerEvent): Point => {
    const bounds = image.current!.getBoundingClientRect();
    return [
      Math.max(
        0,
        Math.min(version!.width, (event.clientX - bounds.left) / scale),
      ),
      Math.max(
        0,
        Math.min(version!.height, (event.clientY - bounds.top) / scale),
      ),
    ];
  };
  const pointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!version || busy) return;
    if (mode === "perspective") {
      const p = point(event);
      if (points.length < 4) setPoints([...points, p]);
      return;
    }
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = {
      point: mode === "pan" ? [event.clientX, event.clientY] : point(event),
      scroll: [stage.current!.scrollLeft, stage.current!.scrollTop],
      type: mode,
    };
    if (mode !== "pan") {
      const [x, y] = point(event);
      setBox([x, y, x, y]);
    }
  };
  const pointerMove = (event: React.PointerEvent) => {
    if (!drag.current || !version) return;
    if (drag.current.type === "pan") {
      stage.current!.scrollLeft =
        drag.current.scroll[0] + drag.current.point[0] - event.clientX;
      stage.current!.scrollTop =
        drag.current.scroll[1] + drag.current.point[1] - event.clientY;
      return;
    }
    const p = point(event);
    if (drag.current.type === "corner") {
      setPoints((old) =>
        old.map((v, i) => (i === drag.current!.corner ? p : v)),
      );
      return;
    }
    const q = drag.current.point;
    setBox([
      Math.min(p[0], q[0]),
      Math.min(p[1], q[1]),
      Math.max(p[0], q[0]),
      Math.max(p[1], q[1]),
    ]);
  };
  const magnify = (delta: number) => {
    setZoom(Math.max(0.1, Math.min(4, scale + delta)));
    setFit(false);
  };
  const choose = (next: typeof mode) => {
    setMode(next);
    setBox(null);
    setPoints([]);
  };
  return (
    <section className="image-workspace" aria-label="图片工作区">
      <div className="image-toolbar">
        <div className="tool-group">
          <Button
            title="拖动图片"
            aria-label="拖动图片"
            appearance={mode === "pan" ? "primary" : "subtle"}
            icon={<Hand size={17} />}
            onClick={() => choose("pan")}
          />
          <Button
            title="裁剪"
            aria-label="裁剪"
            appearance={mode === "crop" ? "primary" : "subtle"}
            icon={<Crop size={17} />}
            onClick={() => choose("crop")}
          />
          <Button
            title="四角透视"
            aria-label="四角透视"
            appearance={mode === "perspective" ? "primary" : "subtle"}
            icon={<Scan size={17} />}
            onClick={() => choose("perspective")}
          />
          <Button
            title="区域重识别"
            aria-label="区域重识别"
            appearance={mode === "region" ? "primary" : "subtle"}
            icon={<Focus size={17} />}
            onClick={() => choose("region")}
          />
        </div>
        <div className="tool-group">
          <Button
            title="顺时针旋转"
            aria-label="顺时针旋转"
            icon={<RotateCw size={17} />}
            appearance="subtle"
            disabled={!version || busy}
            onClick={() => onTransform({ kind: "rotate", degrees: 90 })}
          />
          <Button
            title="增强对比度"
            aria-label="增强对比度"
            icon={<Contrast size={17} />}
            appearance="subtle"
            disabled={!version || busy}
            onClick={() => onTransform({ kind: "contrast", factor: 1.3 })}
          />
        </div>
        <div className="zoom-tools">
          <Button
            aria-label="缩小"
            icon={<ZoomOut size={16} />}
            appearance="subtle"
            onClick={() => magnify(-0.15)}
          />
          <button
            className="zoom-label"
            onClick={() => setFit(true)}
            title="适合窗口"
          >
            {Math.round(scale * 100)}%
          </button>
          <Button
            aria-label="放大"
            icon={<ZoomIn size={16} />}
            appearance="subtle"
            onClick={() => magnify(0.15)}
          />
        </div>
      </div>
      {version && (
        <div className="version-bar">
          <select
            aria-label="图片版本"
            value={version.id}
            onChange={(e) => onVersion(e.target.value)}
          >
            {versions.map((v, i) => (
              <option key={v.id} value={v.id}>
                {i === 0
                  ? "原始图片"
                  : `版本 ${i + 1} · ${operationName(v.operations)}`}
              </option>
            ))}
          </select>
          <span>
            {version.width} × {version.height}
          </span>
          <Button
            size="small"
            appearance="subtle"
            disabled={busy}
            onClick={() => onTransform({ kind: "dewarp" })}
          >
            去弯曲
          </Button>
          {version.parent_id && (
            <Button
              size="small"
              appearance="subtle"
              icon={<CornerUpLeft size={14} />}
              onClick={() => onVersion(versions[0].id)}
            >
              查看原图
            </Button>
          )}
        </div>
      )}
      {mode !== "pan" && (
        <div className="selection-bar">
          <span>
            {mode === "perspective"
              ? `按左上、右上、右下、左下选取四角 (${points.length}/4)`
              : mode === "crop"
                ? "拖动框选裁剪范围"
                : "框选困难区域，用当前所选引擎重新识别"}
          </span>
          <Button
            size="small"
            disabled={
              busy ||
              (mode === "perspective"
                ? points.length !== 4
                : !box || box[2] - box[0] < 8 || box[3] - box[1] < 8)
            }
            appearance="primary"
            onClick={() => {
              if (mode === "perspective")
                onTransform({ kind: "perspective", points });
              else if (mode === "crop") onTransform({ kind: "crop", box });
              else onRegion(box!);
            }}
          >
            应用
          </Button>
          <Button
            size="small"
            appearance="subtle"
            onClick={() => choose("pan")}
          >
            取消
          </Button>
        </div>
      )}
      <div
        ref={stage}
        className={
          "paper-stage " + (mode === "pan" ? "pan-mode" : "select-mode")
        }
      >
        {!version ? (
          <div className="canvas-empty">
            <div className="paper-outline">
              <ImageIcon size={36} />
            </div>
            <h2>让图片里的信息可编辑</h2>
            <p>导入照片，选择识别方式，即可开始校对。</p>
            <span>JPG · PNG · TIFF · WebP · HEIC</span>
          </div>
        ) : loadError ? (
          <div className="canvas-empty">
            <p>{loadError}</p>
          </div>
        ) : !url ? (
          <div className="canvas-empty">正在读取图片…</div>
        ) : (
          <div
            className="paper-placement"
            style={{
              minWidth: version.width * scale + 64,
              minHeight: version.height * scale + 64,
            }}
          >
            <div
              ref={image}
              className="paper-image"
              style={{
                width: version.width * scale,
                height: version.height * scale,
              }}
              onPointerDown={pointerDown}
              onPointerMove={pointerMove}
              onPointerUp={() => {
                drag.current = null;
              }}
              onPointerCancel={() => {
                drag.current = null;
              }}
            >
              <img src={url} alt="当前图片版本" draggable={false} />
              <svg
                className="image-overlay"
                viewBox={`0 0 ${version.width} ${version.height}`}
              >
                {blocks.map(
                  (block, i) =>
                    block.polygon && (
                      <polygon
                        key={i}
                        points={block.polygon.map((p) => p.join(",")).join(" ")}
                        fill={highlight === i ? "#106bcb25" : "transparent"}
                        stroke={highlight === i ? "#126ac9" : "#126ac955"}
                        strokeWidth={highlight === i ? 3 / scale : 1 / scale}
                      />
                    ),
                )}
                {box && (
                  <rect
                    x={box[0]}
                    y={box[1]}
                    width={box[2] - box[0]}
                    height={box[3] - box[1]}
                    fill="#146ac320"
                    stroke="#146ac3"
                    strokeWidth={2 / scale}
                    strokeDasharray={`${6 / scale} ${4 / scale}`}
                  />
                )}
                {points.length > 1 && (
                  <polyline
                    points={(points.length === 4
                      ? [...points, points[0]]
                      : points
                    )
                      .map((p) => p.join(","))
                      .join(" ")}
                    fill={points.length === 4 ? "#126ac922" : "none"}
                    stroke="#126ac9"
                    strokeWidth={2 / scale}
                  />
                )}
                {points.map((p, i) => (
                  <g
                    key={i}
                    onPointerDown={(event) => {
                      event.stopPropagation();
                      event.currentTarget.setPointerCapture(event.pointerId);
                      drag.current = {
                        point: p,
                        scroll: [0, 0],
                        type: "corner",
                        corner: i,
                      };
                    }}
                  >
                    <circle
                      cx={p[0]}
                      cy={p[1]}
                      r={9 / scale}
                      fill="#126ac9"
                      stroke="white"
                      strokeWidth={2 / scale}
                    />
                    <text
                      x={p[0]}
                      y={p[1] + 4 / scale}
                      textAnchor="middle"
                      fontSize={11 / scale}
                      fill="white"
                    >
                      {i + 1}
                    </text>
                  </g>
                ))}
              </svg>
            </div>
          </div>
        )}
      </div>
      <div className="image-footnote">
        原图始终保留
        <span>
          {version ? `${versions.length} 个图像版本` : "本机处理 · 无需上传"}
        </span>
      </div>
    </section>
  );
}
export function operationName(value: string) {
  const kind = JSON.parse(value).kind;
  return (
    (
      {
        import: "原始图片",
        rotate: "旋转",
        crop: "裁剪",
        perspective: "透视校正",
        contrast: "对比度",
        dewarp: "去弯曲",
      } as Record<string, string>
    )[kind] || kind
  );
}
