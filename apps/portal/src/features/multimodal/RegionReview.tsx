import { useRef, useState, type PointerEvent } from 'react'
import { Button, Input, InputNumber, Space, Typography } from 'antd'
import { ForensicImageViewer } from '../forensics/ForensicImageViewer'
import { displayBoxToStored, storedBoxToDisplay, type ForensicGeometry } from '../forensics/geometry'

export type Mark = { box: [number, number, number, number]; note: string }
type Bounds = { box: number[]; label: string }

export function RegionReview({ image, width, height, marks, onChange, boxes = [], forensic }: {
  image: string; width: number; height: number; marks: Mark[]; onChange?: (marks: Mark[]) => void; boxes?: Bounds[];
  forensic?: { heatmap: string; geometry: ForensicGeometry | null; registered: boolean; warning?: string };
}) {
  const geometry = forensic?.geometry
  const displayMarks = geometry ? marks.map(mark => ({ ...mark, box: storedBoxToDisplay(mark.box, geometry) })) : marks
  const changeDisplay = onChange ? (next: Mark[]) => onChange(geometry ? next.map(mark => {
    const existing = displayMarks.find(value => value.box.every((number, index) => number === mark.box[index]))
    return { ...mark, box: existing ? marks[displayMarks.indexOf(existing)].box : displayBoxToStored(mark.box, geometry) }
  }) : next) : undefined
  const svg = useRef<SVGSVGElement>(null), [origin, setOrigin] = useState<[number, number] | null>(null)
  const [draft, setDraft] = useState<Mark['box'] | null>(null)
  const position = (event: PointerEvent) => {
    const area = svg.current!.getBoundingClientRect()
    return [Math.max(0, Math.min(1, (event.clientX - area.left) / area.width)), Math.max(0, Math.min(1, (event.clientY - area.top) / area.height))] as [number, number]
  }
  const coordinates = (end: [number, number]): Mark['box'] => [Math.min(origin![0], end[0]), Math.min(origin![1], end[1]), Math.max(origin![0], end[0]), Math.max(origin![1], end[1])]
  return <div className="mm-region-review" style={forensic ? undefined : { maxWidth: `min(100%, calc(65vh * ${width / height}))` }}>
    {forensic ? <ForensicImageViewer image={image} heatmap={forensic.heatmap}
      width={geometry?.display_size[0] || width} height={geometry?.display_size[1] || height}
      registered={forensic.registered} warning={forensic.warning} marks={displayMarks} onChange={changeDisplay} heatmapLabel="CAT-Net 可疑区域热图" /> : <svg ref={svg} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={onChange ? '在原图上拖动标注可疑区域' : '原件与定位区域'}
      style={{ touchAction: onChange ? 'none' : 'auto', cursor: onChange ? 'crosshair' : 'default' }}
      onPointerDown={event => { if (!onChange || marks.length >= 200) return; event.currentTarget.setPointerCapture(event.pointerId); setOrigin(position(event)); setDraft(null) }}
      onPointerMove={event => { if (origin) setDraft(coordinates(position(event))) }}
      onPointerUp={event => { if (origin && onChange) { const box = coordinates(position(event)); if (box[2] - box[0] > .005 && box[3] - box[1] > .005) onChange([...marks, { box, note: '待复核区域' }]) } setOrigin(null); setDraft(null) }}
      onPointerCancel={() => { setOrigin(null); setDraft(null) }}>
      <image href={image} width={width} height={height} />
      {boxes.map((region, index) => <g key={index}><rect x={region.box[0]} y={region.box[1]} width={region.box[2] - region.box[0]} height={region.box[3] - region.box[1]} fill="none" stroke="#137448" strokeWidth={2} vectorEffect="non-scaling-stroke" /><text x={region.box[0] + 4} y={region.box[1] + Math.max(16, height / 40)} fill="#137448" fontSize={Math.max(16, height / 40)}>{region.label}</text></g>)}
      {[...marks, ...(draft ? [{ box: draft, note: '' }] : [])].map((region, index) => <rect key={index} x={region.box[0] * width} y={region.box[1] * height}
        width={(region.box[2] - region.box[0]) * width} height={(region.box[3] - region.box[1]) * height} fill="#dc861d22" stroke="#c46613" strokeWidth={2} vectorEffect="non-scaling-stroke" />)}
    </svg>}
    {onChange && <><Typography.Paragraph type="secondary">拖动图像标注；也可添加区域并输入当前显示图像的边界百分比。区域说明与复核版本一起保存。</Typography.Paragraph>
      <Button disabled={marks.length >= 200} onClick={() => changeDisplay?.([...displayMarks, { box: [.1, .1, .4, .4], note: '待复核区域' }])}>添加可疑区域</Button></>}
    {displayMarks.map((region, index) => <div className="mm-mark" key={index}><Space wrap><Typography.Text>区域 {index + 1}</Typography.Text>
      {region.box.map((number, coordinate) => <label key={coordinate}>{['左', '上', '右', '下'][coordinate]} % <InputNumber aria-label={`区域${index + 1}${['左', '上', '右', '下'][coordinate]}边界百分比`} min={0} max={100} precision={2} value={number * 100} disabled={!onChange}
        onChange={value => changeDisplay?.(displayMarks.map((mark, i) => i === index ? { ...mark, box: mark.box.map((n, c) => c === coordinate ? Number(value || 0) / 100 : n) as Mark['box'] } : mark))} /></label>)}
      {onChange && <Button danger onClick={() => changeDisplay?.(displayMarks.filter((_, i) => i !== index))}>删除</Button>}</Space>
      <Input.TextArea aria-label={`区域${index + 1}说明`} maxLength={2000} value={region.note} disabled={!onChange} onChange={event => changeDisplay?.(displayMarks.map((mark, i) => i === index ? { ...mark, note: event.target.value } : mark))} />
    </div>)}
  </div>
}
