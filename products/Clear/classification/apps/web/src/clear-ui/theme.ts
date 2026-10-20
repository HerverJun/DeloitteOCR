/** Static Clear UI contract v1.3.0; adapt through each existing component library. */
export const CLEAR_UI_VERSION = '1.3.0'
export const clearTheme = {
  header: '#ffffff', navigation: '#fafbfa', navigationText: '#323b35', navigationMuted: '#6e7971', workspace: '#f5f7f8',
  panel: '#ffffff', subtle: '#f0f3f4', text: '#1d272e', muted: '#596873', border: '#e1e6e9',
  accent: '#86bc25', accentText: '#17200d', primaryText: '#ffffff', primary: '#527a17',
  primaryHover: '#446714', primaryActive: '#385510', selected: '#eff5e7',
  link: '#527a17', focus: '#527a17', success: '#527a17', warning: '#8a641e', error: '#b84646', info: '#56697e',
  infoBg: '#f0f4f7', infoBorder: '#d8e1e8', warningBg: '#fbf6ea', errorBg: '#fdf1f1',
  controlHeight: 36, primaryHeight: 40, headerHeight: 56, pagePadding: 24,
  fontSize: 14, captionSize: 13, titleSize: 28, panelTitleSize: 18, lineHeight: 1.6,
  panelRadius: 12, controlRadius: 8, motionFast: 0.16, motionNormal: 0.2,
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
