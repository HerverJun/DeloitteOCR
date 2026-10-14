import { useEffect, useRef, useState, type PointerEvent } from 'react'
import { Alert, Button, Space, Typography } from 'antd'
import type { ImageSize, NormalizedBox } from './geometry'
import './ForensicImageViewer.css'

export type ViewerMark = { box: NormalizedBox; note: string }
type Mode = 'original' | 'heatmap' | 'overlay'
type LoadedImage = { source: string; size: ImageSize | null; error: boolean }
type Props = {
  image: string; heatmap?: string; width: number; height: number; registered?: boolean; warning?: string
  marks?: ViewerMark[]; onChange?: (marks: ViewerMark[]) => void; heatmapLabel?: string
}

export function ForensicImageViewer({ image, heatmap, width, height, registered = false, warning,
  marks = [], onChange, heatmapLabel = '取证热图' }: Props) {
  const viewport = useRef<HTMLDivElement>(null), svg = useRef<SVGSVGElement>(null)
  const [mode, setMode] = useState<Mode>('original'), [opacity, setOpacity] = useState(.5)
  const [zoom, setZoom] = useState<number | 'fit'>('fit'), [available, setAvailable] = useState<ImageSize>([800, 480])
  const [originalLoad, setOriginalLoad] = useState<LoadedImage>({ source: '', size: null, error: false })
  const [mapLoad, setMapLoad] = useState<LoadedImage>({ source: '', size: null, error: false })
  const originalSize = originalLoad.source === image ? originalLoad.size : null
  const mapSize = mapLoad.source === heatmap ? mapLoad.size : null
  const imageError = originalLoad.source === image && originalLoad.error
  const mapError = mapLoad.source === heatmap && mapLoad.error

  const [origin, setOrigin] = useState<[number, number] | null>(null), [draft, setDraft] = useState<NormalizedBox | null>(null)


  useEffect(() => { setMode('original'); setZoom('fit'); setOrigin(null); setDraft(null) }, [image, heatmap])
  useEffect(() => {
    const area = viewport.current
    if (!area) return
    const measure = () => setAvailable([area.clientWidth || 800, area.clientHeight || 480])
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(area)
    return () => observer.disconnect()
  }, [])
  const originalMatches = !!originalSize && originalSize[0] === width && originalSize[1] === height
  const mapMatches = !!mapSize && mapSize[0] === width && mapSize[1] === height
  const canRegister = registered && originalMatches && mapMatches && !imageError && !mapError
  const effectiveMode = mode === 'overlay' && !canRegister ? 'original' : mode
  const independentMap = effectiveMode === 'heatmap' && !canRegister
  const independentOriginal = effectiveMode === 'original' && !!originalSize && !originalMatches
  const displaySize = independentMap ? mapSize || [width, height] : independentOriginal ? originalSize! : [width, height]
  const scale = zoom === 'fit' ? Math.max(.001, Math.min((available[0] - 2) / displaySize[0], (available[1] - 2) / displaySize[1])) : zoom
  const editable = !!onChange && originalMatches && !imageError && !independentMap && !independentOriginal
  const mismatch = registered && ((!!originalSize && !originalMatches) || (!!mapSize && !mapMatches))
  const position = (event: PointerEvent): [number, number] | null => {
    const area = svg.current?.getBoundingClientRect()
    if (!area?.width || !area.height) return null
    return [Math.max(0, Math.min(1, (event.clientX - area.left) / area.width)),
      Math.max(0, Math.min(1, (event.clientY - area.top) / area.height))]
  }
  const rectangle = (end: [number, number]): NormalizedBox =>
    [Math.min(origin![0], end[0]), Math.min(origin![1], end[1]), Math.max(origin![0], end[0]), Math.max(origin![1], end[1])]
  function finish(event: PointerEvent) {
    const end = position(event)
    if (origin && end && editable) {
      const box = rectangle(end)
      if (box[2] - box[0] > .005 && box[3] - box[1] > .005) onChange?.([...marks, { box, note: '待复核区域' }])
    }
    setOrigin(null); setDraft(null)
  }
  return <section className="forensic-viewer" aria-label="图像取证对照查看器">
    <Space wrap className="forensic-viewer-toolbar">
      <Space.Compact aria-label="图像图层">
        <Button aria-label="原图" aria-pressed={effectiveMode === 'original'} onClick={() => setMode('original')}>原图</Button>
        <Button aria-label="热图" aria-pressed={effectiveMode === 'heatmap'} disabled={!heatmap || mapError} onClick={() => setMode('heatmap')}>热图</Button>
        <Button aria-label="叠加" aria-pressed={effectiveMode === 'overlay'} disabled={!canRegister} onClick={() => setMode('overlay')}>叠加</Button>
      </Space.Compact>
      <label className="forensic-opacity">热图透明度
        <input aria-label="热图透明度" type="range" min="0" max="100" value={Math.round(opacity * 100)} disabled={!canRegister}
          onChange={event => setOpacity(Number(event.target.value) / 100)} /><output>{Math.round(opacity * 100)}%</output>
      </label>
      <Space.Compact>
        <Button aria-pressed={zoom === 'fit'} onClick={() => setZoom('fit')}>适应窗口</Button>
        <Button aria-pressed={zoom === 1} onClick={() => setZoom(1)}>1:1</Button>
        <Button aria-label="缩小图像" onClick={() => setZoom(Math.max(.01, scale / 1.25))}>−</Button>
        <Button aria-label="放大图像" onClick={() => setZoom(Math.min(8, scale * 1.25))}>＋</Button>
      </Space.Compact><Typography.Text type="secondary">{Math.round(scale * 100)}%</Typography.Text>
    </Space>
    {warning && <Alert type="warning" title={warning} />}
    {mismatch && <Alert type="warning" title="预览与热图的实际尺寸或坐标信息不一致，已停用叠加；热图仅独立查看。" />}
    {(imageError || mapError) && <Alert type="warning" title={imageError ? '原图预览加载失败，请下载原件核查。' : '热图加载失败，当前保留原图。'} />}
    <img key={image} className="forensic-image-probe" alt="" aria-hidden="true" src={image}
      onLoad={event => setOriginalLoad({ source: image, size: [event.currentTarget.naturalWidth, event.currentTarget.naturalHeight], error: false })} onError={() => setOriginalLoad({ source: image, size: null, error: true })} />
    {heatmap && <img key={heatmap} className="forensic-image-probe" alt="" aria-hidden="true" src={heatmap}
      onLoad={event => setMapLoad({ source: heatmap, size: [event.currentTarget.naturalWidth, event.currentTarget.naturalHeight], error: false })} onError={() => setMapLoad({ source: heatmap, size: null, error: true })} />}
    <div className="forensic-viewer-viewport" ref={viewport} tabIndex={0} aria-label="图像滚动区域">
      {independentMap || independentOriginal
        ? <img className="forensic-independent-image" src={independentMap ? heatmap : image} alt={independentMap ? heatmapLabel + '（独立查看）' : '原图预览'}
          style={{ width: displaySize[0] * scale, height: displaySize[1] * scale }} />
        : <svg ref={svg} role="img" aria-label={editable ? '在原图上拖动标注可疑区域' : effectiveMode === 'heatmap' ? heatmapLabel : '原件与定位区域'}
          viewBox={`0 0 ${width} ${height}`} width={width * scale} height={height * scale}
          style={{ width: width * scale, height: height * scale, touchAction: editable ? 'none' : 'auto', cursor: editable ? 'crosshair' : 'default' }}
          onPointerDown={event => { const start = position(event); if (!editable || !start || marks.length >= 200 || event.button !== 0) return
            event.currentTarget.setPointerCapture?.(event.pointerId); setOrigin(start); setDraft(null) }}
          onPointerMove={event => { const end = position(event); if (origin && end) setDraft(rectangle(end)) }}
          onPointerUp={finish} onPointerCancel={() => { setOrigin(null); setDraft(null) }}>
          {effectiveMode !== 'heatmap' && <image data-layer="original" href={image} width={width} height={height} preserveAspectRatio="none" />}
          {effectiveMode !== 'original' && heatmap && <image data-layer="heatmap" href={heatmap} width={width} height={height}
            preserveAspectRatio="none" opacity={effectiveMode === 'overlay' ? opacity : 1} />}
          {[...marks, ...(draft ? [{ box: draft, note: '' }] : [])].map((mark, index) => <rect key={index} data-region={index}
            x={mark.box[0] * width} y={mark.box[1] * height} width={(mark.box[2] - mark.box[0]) * width}
            height={(mark.box[3] - mark.box[1]) * height} fill="#dc861d22" stroke="#c46613" strokeWidth={2} vectorEffect="non-scaling-stroke" />)}
        </svg>}
    </div>
    <Typography.Paragraph className="forensic-viewer-caption" type="secondary">
      {effectiveMode === 'overlay' ? '原图与热图叠加' : effectiveMode === 'heatmap' ? heatmapLabel + (independentMap ? ' · 独立查看' : '') : '原图预览'}
      {canRegister ? ' · 图层与区域共用 EXIF 校正坐标' : ''} · 放大后可滚动查看细节。
    </Typography.Paragraph>
  </section>
}
