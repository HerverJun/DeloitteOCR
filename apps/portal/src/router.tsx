import { createBrowserRouter, type RouteObject } from 'react-router-dom'
import { QueryClient } from '@tanstack/react-query'
import type { ThemeConfig } from 'antd'
import { App, Home, Workspaces, Applications, Application, Placeholder, Missing } from './App'
import { TaskCenter } from './features/jobs/TaskCenter'
import { DataWorkbench } from './features/data/DataWorkbench'
import { AssetLibrary } from './features/assets/AssetLibrary'
import { ArtifactCenter } from './features/artifacts/ArtifactCenter'
import { OcrWorkflow } from './features/ocr/OcrWorkflow'
import { ClassificationWorkflow } from './features/classification/ClassificationWorkflow'
import { BatchCenter } from './features/batches/BatchCenter'
import { clearTheme } from './clear-ui/theme'

export const theme: ThemeConfig = {
  token: { colorPrimary: clearTheme.accent, colorLink: clearTheme.link, colorLinkHover: clearTheme.link,
    colorText: clearTheme.text, colorTextSecondary: clearTheme.muted, colorBgBase: clearTheme.panel,
    colorBgLayout: clearTheme.workspace, colorBorder: clearTheme.border, colorBorderSecondary: clearTheme.border,
    borderRadius: clearTheme.controlRadius, borderRadiusLG: clearTheme.panelRadius,
    controlHeight: clearTheme.controlHeight, fontSize: clearTheme.fontSize, fontFamily: clearTheme.fontFamily },
  components: {
    Layout: { siderBg: clearTheme.navigation, bodyBg: clearTheme.workspace, headerBg: clearTheme.header, headerHeight: clearTheme.headerHeight },
    Menu: { darkItemBg: clearTheme.navigation, darkItemSelectedBg: '#29341f', darkItemSelectedColor: '#d2eb9e',
      darkItemColor: '#c6cec8', darkItemHoverBg: '#242b24', itemHeight: clearTheme.controlHeight, itemBorderRadius: clearTheme.controlRadius },
    Button: { primaryColor: clearTheme.accentText, dangerColor: '#ffffff', defaultBorderColor: clearTheme.border },
    Card: { headerFontSize: clearTheme.panelTitleSize, headerHeight: 44, bodyPadding: 16 },
    Typography: { fontSizeHeading1: clearTheme.titleSize, fontSizeHeading3: clearTheme.panelTitleSize, fontSizeHeading4: clearTheme.panelTitleSize },
    Table: { cellPaddingBlockSM: 8, cellPaddingInlineSM: 12 },
  },
}

export const queryClient = new QueryClient({ defaultOptions: {
  queries: { retry: false, staleTime: 15_000, refetchOnWindowFocus: false },
} })

export const routes: RouteObject[] = [{
  path: '/', element: <App />, children: [
    { index: true, element: <Home /> },
    { path: 'workspaces', element: <Workspaces /> },
    { path: 'batches', element: <BatchCenter /> },
    { path: 'apps', element: <Applications /> },
    { path: 'apps/data', element: <DataWorkbench /> },
    { path: 'apps/ocr/workflow', element: <OcrWorkflow /> },
    { path: 'apps/classification/workflow', element: <ClassificationWorkflow /> },
    { path: 'apps/:appId', element: <Application /> },
    { path: 'assets', element: <AssetLibrary /> },
    { path: 'jobs', element: <TaskCenter /> },
    { path: 'artifacts', element: <ArtifactCenter /> },
    { path: 'settings', element: <Placeholder title="设置" description="门户设置尚未开放。应用安装和平台数据目录由本机管理员配置。" /> },
    { path: '*', element: <Missing /> },
  ],
}]
export const router = createBrowserRouter(routes)
