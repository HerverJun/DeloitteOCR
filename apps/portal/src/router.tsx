import { createBrowserRouter, type RouteObject } from 'react-router-dom'
import { QueryClient } from '@tanstack/react-query'
import type { ThemeConfig } from 'antd'
import { App, Home, Workspaces, Applications, Application, Placeholder, Missing } from './App'
import { TaskCenter } from './features/jobs/TaskCenter'

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
    { path: 'apps/:appId', element: <Application /> },
    { path: 'assets', element: <Placeholder title="资料" description="资料汇集与跨工具引用尚未开放。原有工具中的资料仍由各工具管理。" /> },
    { path: 'jobs', element: <TaskCenter /> },
    { path: 'artifacts', element: <Placeholder title="成果" description="统一成果目录尚未开放。请在相应工具中查看和导出成果。" /> },
    { path: 'settings', element: <Placeholder title="设置" description="门户设置尚未开放。应用安装和平台数据目录由本机管理员配置。" /> },
    { path: '*', element: <Missing /> },
  ],
}]
export const router = createBrowserRouter(routes)
