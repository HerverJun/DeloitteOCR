/** Static Clear UI contract v1.3.0; adapt through each existing component library. */
export const CLEAR_UI_VERSION = '1.3.0'
export const clearTheme = {
  header: '#ffffff', navigation: '#fafbfa', workspace: '#F5F7F8',
  panel: '#FFFFFF', subtle: '#F0F3F4', text: '#1D272E', muted: '#596873', border: '#E1E6E9',
  accent: '#86BC25', accentText: '#17200D', primary: '#527A17', primaryText: '#FFFFFF',
  primaryHover: '#446714', primaryPressed: '#385510',
  link: '#527A17', focus: '#527A17', selected: '#EFF5E7',
  controlHeight: 36, primaryHeight: 40, headerHeight: 56, pagePadding: 24,
  fontSize: 14, captionSize: 13, titleSize: 28, panelTitleSize: 18,
  panelRadius: 12, controlRadius: 8, motionFast: 160, motionBase: 200,
  panelPadding: 20, workbenchPadding: 16, workbenchPanelPadding: 16, toolbarGap: 8,
  fontFamily: "'Clear Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', sans-serif",
} as const
export const brands = {
  platform: 'Clear 智能审计', ocr: 'ClearOCR',
  classification: 'ClearClassify', data: 'ClearData',
} as const
export function brandName(appId: string, fallback = appId): string {
  return appId === 'ocr' ? brands.ocr : appId === 'classification' ? brands.classification : fallback
}
