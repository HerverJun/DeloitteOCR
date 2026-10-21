/** Static Clear UI contract v1.3.0; adapt through each existing component library. */
export const CLEAR_UI_VERSION = '1.3.0'
export const clearTheme = {
  header: '#ffffff', navigation: '#fafbfa', workspace: '#f6f8f7',
  panel: '#ffffff', subtle: '#f0f3f4', selected: '#eff5e7',
  text: '#1d272e', muted: '#596873', border: '#e1e6e9',
  accent: '#86bc25', accentText: '#17200d',
  primary: '#527a17', primaryText: '#ffffff', link: '#527a17', focus: '#527a17',
  success: '#527a17', warning: '#8a641e', error: '#b84646', info: '#56697e',
  navigationText: '#323b35', navigationMuted: '#6e7971',
  controlHeight: 36, primaryHeight: 40, headerHeight: 56,
  fontSize: 14, captionSize: 13, titleSize: 28, panelTitleSize: 18,
  panelRadius: 12, controlRadius: 8, pagePadding: 24,
  panelPadding: 20, workbenchPadding: 16, workbenchPanelPadding: 16, toolbarGap: 8,
  motionFast: '0.16s', motionNormal: '0.2s',
  fontFamily: "'Clear Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', sans-serif",
} as const
export const brands = {
  platform: 'Clear 智能审计', ocr: 'ClearOCR',
  classification: 'ClearClassify', data: 'ClearData', forensics: 'ClearForensics',
}
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
