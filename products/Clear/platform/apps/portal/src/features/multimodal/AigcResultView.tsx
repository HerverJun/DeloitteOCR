import { Alert, Image, Space, Tag, Typography } from 'antd'
import { Link } from 'react-router-dom'
import type { AigcImageResult } from '../../../../../packages/contracts/types.generated'
export type { AigcImageResult } from '../../../../../packages/contracts/types.generated'

const { Paragraph, Text } = Typography

const stateLabels = {
  signal_detected: '检出生成相关信号',
  no_significant_signal: '未发现显著信号',
  unable_to_assess: '无法判断',
}
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
const scoreText = (value: number) => value.toFixed(6)
const scopeLabels: Record<string, string> = { whole_image: '整图分类', document: '文档图片', general_image: '通用图片' }

export function AigcResultView({ value, preview, sourceName, workspaceId, assetId, model, performance }: {
  value?: AigcImageResult; preview: string; sourceName: string; workspaceId: string; assetId?: string
  model?: Record<string, unknown>; performance?: Record<string, unknown>
}) {
  const hasScore = finite(value?.raw_score)
  const hasThreshold = finite(value?.threshold) && !!value?.calibration
  // An uncalibrated model score cannot establish either detection or a negative result.
  const state = hasScore && hasThreshold && value && value.state in stateLabels ? value.state : 'unable_to_assess'
  const sameAsset = new URLSearchParams({ workspace: workspaceId, ...(assetId ? { asset: assetId } : {}) })
  const calibrationId = value?.calibration?.calibration_id
  return <section className="mm-aigc" aria-label="整图生成检测结果">
    <div className="mm-aigc-result">
      <figure className="mm-aigc-preview"><figcaption>分析原图 · {sourceName}</figcaption>
        <Image src={preview} alt={`整图生成检测原图：${sourceName}`} className="mm-aigc-image" />
      </figure>
      <div className="mm-aigc-summary">
        <Space wrap><Tag color="warning">实验能力</Tag><Tag>{hasThreshold ? '已记录校准' : '未校准'}</Tag></Space>
        <Alert type={state === 'signal_detected' ? 'warning' : 'info'} title={stateLabels[state]}
          description={!hasThreshold ? '当前仅提供模型原始信号。低误报阈值尚未完成独立校准，因此无法作出整图生成判断。' : state === 'no_significant_signal' ? '在此模型与记录阈值下未发现显著生成信号；该结果不能证明图片来源真实。' : '请结合图片来源和人工复核评估。整图信号不能指出具体修改区域。'} />
        <div className="mm-score" aria-label="模型原始生成信号"><span>原始生成信号</span><strong>{hasScore ? scoreText(value!.raw_score!) : '未取得'}</strong>
          <Text type="secondary">{value?.score_direction === 'higher_is_generated' ? '数值越大，生成相关信号越强' : '信号方向未记录'}</Text></div>
        <Paragraph type="secondary">此分数未经概率校准。两类分析结果分别复核，整图分类不提供定位热图。</Paragraph>
        <dl className="mm-aigc-metadata">
          <div><dt>检测阈值</dt><dd>{hasThreshold ? scoreText(value!.threshold!) : '未设置'}</dd></div>
          <div><dt>校准记录</dt><dd>{typeof calibrationId === 'string' ? calibrationId : '尚无独立校准记录'}</dd></div>
          <div><dt>检查范围（待验证）</dt><dd>{value?.scope?.length ? value.scope.map(scope => scopeLabels[scope] || scope).join('、') : '尚未完成范围验证'}</dd></div>
          <div><dt>预处理版本</dt><dd>{value?.preprocessing_version || '未记录'}</dd></div>
        </dl>
      </div>
    </div>
    {!!value?.limitations?.length && <ul className="mm-limitations">{value.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>}
    <Space wrap><Link to={`/apps/forensics/deep?${sameAsset}`}>使用 SAFIRE 定位同一原图的局部线索</Link>
      <Link to={`/apps/forensics/document?${sameAsset}`}>使用文档篡改排查</Link></Space>
    <details><summary>处理、校准与模型记录</summary><pre className="mm-log">{JSON.stringify({ model, performance, calibration: value?.calibration, preprocessing_version: value?.preprocessing_version }, null, 2)}</pre></details>
  </section>
}
