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

export const theme: ThemeConfig = {
  token: { colorPrimary: '#236759', colorText: '#172522', colorBgBase: '#f7f9f8',
    colorBorder: '#d9e2de', borderRadius: 8, fontFamily: "'Segoe UI', 'Microsoft YaHei', sans-serif" },
  components: { Layout: { siderBg: '#182622', bodyBg: '#f7f9f8', headerBg: '#f7f9f8' },
    Menu: { darkItemBg: '#182622', darkItemSelectedBg: '#295447', darkItemColor: '#b8c9c2' } },
}

export const queryClient = new QueryClient({ defaultOptions: {
  queries: { retry: false, staleTime: 15_000, refetchOnWindowFocus: false },
} })

export const routes: RouteObject[] = [{
  path: '/', element: <App />, children: [
    { index: true, element: <Home /> },
    { path: 'workspaces', element: <Workspaces /> },
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
