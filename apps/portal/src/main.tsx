import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { RouterProvider } from 'react-router-dom'
import { QueryClientProvider } from '@tanstack/react-query'
import { ConfigProvider } from 'antd'
import { router, queryClient, theme } from './router'
import './clear-ui/tokens.css'
import './style.css'

createRoot(document.getElementById('root')!).render(
  <StrictMode><ConfigProvider theme={theme}><QueryClientProvider client={queryClient}>
    <RouterProvider router={router} />
  </QueryClientProvider></ConfigProvider></StrictMode>,
)
