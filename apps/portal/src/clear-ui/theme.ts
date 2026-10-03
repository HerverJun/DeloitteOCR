/** Static Clear UI contract v1.0.0; adapt through each existing component library. */
export const CLEAR_UI_VERSION = '1.0.0'
export const clearTheme = {
  header: '#070908', navigation: '#171b18', workspace: '#f4f6f4',
  panel: '#ffffff', text: '#202823', muted: '#66706a', border: '#e2e7df',
  accent: '#86bc25', accentText: '#17200d', link: '#386a12', focus: '#386a12',
  controlHeight: 34, primaryHeight: 38, headerHeight: 44,
  fontSize: 14, captionSize: 12, titleSize: 23, panelTitleSize: 16,
  panelRadius: 10, controlRadius: 6,
  fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', sans-serif",
} as const
export const brands = {
  platform: 'Clear Workbench', ocr: 'ClearOCR',
  classification: 'ClearClassify', data: 'ClearData', forensics: 'ClearForensics',
} as const
export function brandName(appId: string, fallback = appId): string {
  return appId === 'ocr' ? brands.ocr : appId === 'classification' ? brands.classification :
    appId === 'data' ? brands.data : appId === 'forensics' ? brands.forensics : fallback
}
export const applicationDescriptions: Record<string, { purpose: string; description: string }> = {
  data: { purpose: '数据清洗与比对', description: '导入表格，清洗与比较差异，导出结果和来源清单。' },
  classification: { purpose: '图像分类与复核', description: '整理图像资料，执行分类并人工复核归类结果。' },
  ocr: { purpose: '文档识别与校对', description: '识别文档文字和字段，校对结果并保留原文来源。' },
  forensics: { purpose: '图像取证与审阅', description: '检查图像篡改线索，审阅原图与热图，形成报告和审计证据。' },
}
