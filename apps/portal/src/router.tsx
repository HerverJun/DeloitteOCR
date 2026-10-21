import { lazy } from 'react'
import { createBrowserRouter, Navigate, useLocation, type RouteObject } from 'react-router-dom'
export { queryClient } from './queryClient'
import type { ThemeConfig } from 'antd'
import { App, Home, AgentHome, Workspaces, Applications, Application, Missing } from './App'
const TaskCenter = lazy(() => import('./features/jobs/TaskCenter').then(module => ({ default: module.TaskCenter })))
const DataWorkbench = lazy(() => import('./features/data/DataWorkbench').then(module => ({ default: module.DataWorkbench })))
const AssetLibrary = lazy(() => import('./features/assets/AssetLibrary').then(module => ({ default: module.AssetLibrary })))
const ArtifactCenter = lazy(() => import('./features/artifacts/ArtifactCenter').then(module => ({ default: module.ArtifactCenter })))
const OcrWorkflow = lazy(() => import('./features/ocr/OcrWorkflow').then(module => ({ default: module.OcrWorkflow })))
const DocumentWorkbench = lazy(() => import('./features/documents/DocumentWorkbench').then(module => ({ default: module.DocumentWorkbench })))
const ClassificationWorkflow = lazy(() => import('./features/classification/ClassificationWorkflow').then(module => ({ default: module.ClassificationWorkflow })))
const BatchCenter = lazy(() => import('./features/batches/BatchCenter').then(module => ({ default: module.BatchCenter })))
const AuditWorkbench = lazy(() => import('./features/audit/AuditWorkbench').then(module => ({ default: module.AuditWorkbench })))
const TodoCenter = lazy(() => import('./features/todos/TodoCenter').then(module => ({ default: module.TodoCenter })))
const ForensicWorkbench = lazy(() => import('./features/forensics/ForensicWorkbench').then(module => ({ default: module.ForensicWorkbench })))
const MultimodalWorkbench = lazy(() => import('./features/multimodal/MultimodalWorkbench').then(module => ({ default: module.MultimodalWorkbench })))
import { clearTheme } from './clear-ui/theme'
const QuickNativeWorkbench = lazy(() => import('./features/start/QuickNativeWorkbench').then(module => ({ default: module.QuickNativeWorkbench })))
const PlatformSettings = lazy(() => import('./features/agent/PlatformSettings').then(module => ({ default: module.PlatformSettings })))
const FindingsWorkspace = lazy(() => import('./features/agent/FindingsWorkspace').then(module => ({ default: module.FindingsWorkspace })))
const Projects = lazy(() => import('./features/agent/ProjectHome').then(module => ({ default: module.Projects })))
const AuditHelp = lazy(() => import('./features/agent/AuditHelp').then(module => ({ default: module.AuditHelp })))
import { SessionGate } from './features/tasks/session'
const AccountSettings = lazy(() => import('./features/tasks/AccountSettings').then(module => ({ default: module.AccountSettings })))

export const theme: ThemeConfig = {
  token: { colorPrimary: clearTheme.primary, colorLink: clearTheme.link, colorLinkHover: clearTheme.link,
    colorText: clearTheme.text, colorTextSecondary: clearTheme.muted, colorTextDescription: clearTheme.muted, colorBgBase: clearTheme.panel,
    colorBgLayout: clearTheme.workspace, colorBorder: clearTheme.border, colorBorderSecondary: clearTheme.border,
    colorPrimaryBg: clearTheme.selected, colorFillAlter: clearTheme.subtle,
    borderRadius: clearTheme.controlRadius, borderRadiusLG: clearTheme.panelRadius,
    colorInfoBg: '#f0f4f7', colorInfoBorder: '#d8e1e8',
    colorSuccessBg: clearTheme.selected, colorWarningBg: '#fbf6ea', colorErrorBg: '#fdf1f1',
    colorSuccess: clearTheme.success, colorWarning: clearTheme.warning, colorError: clearTheme.error, colorInfo: clearTheme.info,
    motionDurationFast: clearTheme.motionFast, motionDurationMid: clearTheme.motionNormal,
    controlHeight: clearTheme.controlHeight, fontSize: clearTheme.fontSize, fontFamily: clearTheme.fontFamily },
  components: {
    Layout: { siderBg: clearTheme.navigation, bodyBg: clearTheme.workspace, headerBg: clearTheme.header, headerHeight: clearTheme.headerHeight },
    Menu: { darkItemBg: clearTheme.navigation, darkItemSelectedBg: '#273329', darkItemSelectedColor: '#d2eb9e',
      darkItemColor: clearTheme.navigationText, darkItemHoverBg: '#222c31', itemHeight: clearTheme.controlHeight, itemBorderRadius: clearTheme.controlRadius },
    Button: { primaryColor: clearTheme.primaryText, dangerColor: '#ffffff', defaultBorderColor: clearTheme.border,
      defaultShadow: 'none', primaryShadow: 'none' },
    Card: { headerFontSize: clearTheme.panelTitleSize, headerHeight: 48, bodyPadding: clearTheme.panelPadding },
    Form: { itemMarginBottom: 16, verticalLabelPadding: '0 0 6px' },
    Typography: { fontSizeHeading1: clearTheme.titleSize, fontSizeHeading2: clearTheme.titleSize, fontSizeHeading3: clearTheme.panelTitleSize, fontSizeHeading4: clearTheme.panelTitleSize },
    Table: { cellPaddingBlockSM: 8, cellPaddingInlineSM: 12, headerBg: clearTheme.subtle,
      headerColor: clearTheme.muted, rowSelectedBg: clearTheme.selected },
  },
}


function LegacyForensicsRedirect() {
  const { search, hash } = useLocation()
  return <Navigate replace to={`/apps/forensics${search}${hash}`} />
}

function LegacyDataRedirect() {
  const { search, hash } = useLocation()
  return <Navigate replace to={`/apps/data${search}${hash}`} />
}

function ManualToolsRedirect() {
  const { search, hash } = useLocation()
  const tool = new URLSearchParams(search).get('tool')
  const path = tool === 'ocr' || tool === 'classification' ? `/apps/${tool}/start` : tool === 'data' || tool === 'forensics' ? `/apps/${tool}` : '/apps'
  return <Navigate replace to={`${path}${search}${hash}`} />
}

export const routes: RouteObject[] = [{
  path: '/', element: <SessionGate><App /></SessionGate>, children: [
    { index: true, element: <Home /> },
    { path: 'projects', element: <Projects /> },
    { path: 'findings', element: <FindingsWorkspace /> },
    { path: 'help', element: <AuditHelp /> },
    { path: 'workspaces', element: <Workspaces /> },
    { path: 'batches', element: <BatchCenter /> },
    { path: 'audit', element: <AuditWorkbench /> },
    { path: 'todos', element: <TodoCenter /> },
    { path: 'forensics', element: <LegacyForensicsRedirect /> },
    { path: 'data', element: <LegacyDataRedirect /> },
    { path: 'apps', element: <Applications /> },
    { path: 'apps/data', element: <DataWorkbench /> },
    { path: 'apps/forensics', element: <ForensicWorkbench /> },
    { path: 'apps/face', element: <MultimodalWorkbench kind="face" /> },
    { path: 'apps/audio', element: <MultimodalWorkbench kind="audio" /> },
    { path: 'apps/ocr/seals', element: <MultimodalWorkbench kind="seal" /> },
    { path: 'apps/forensics/deep', element: <MultimodalWorkbench kind="catnet" /> },
    { path: 'apps/ocr/start', element: <QuickNativeWorkbench appId="ocr" /> },
    { path: 'apps/classification/start', element: <QuickNativeWorkbench appId="classification" /> },
    { path: 'apps/ocr/workflow', element: <OcrWorkflow /> },
    { path: 'apps/ocr/documents', element: <DocumentWorkbench /> },
    { path: 'apps/classification/workflow', element: <ClassificationWorkflow /> },
    { path: 'apps/:appId', element: <Application /> },
    { path: 'assets', element: <AssetLibrary /> },
    { path: 'jobs', element: <TaskCenter /> },
    { path: 'artifacts', element: <ArtifactCenter /> },
    { path: 'agent', element: <AgentHome /> },
    { path: 'manual', element: <ManualToolsRedirect /> },
    { path: 'settings', element: <PlatformSettings /> },
    { path: 'settings/accounts', element: <AccountSettings /> },
    { path: '*', element: <Missing /> },
  ],
}]
export const router = createBrowserRouter(routes)
