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
  classification: 'ClearClassify', data: 'ClearData',
} as const
export function brandName(appId: string, fallback = appId): string {
  return appId === 'ocr' ? brands.ocr : appId === 'classification' ? brands.classification : fallback
}
