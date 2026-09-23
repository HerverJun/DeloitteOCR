import React from 'react';
import '@ant-design/v5-patch-for-react-19';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { Workbench } from './workbench';
import './style.css';

const client = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 1500, refetchOnWindowFocus: false } } });

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={client}>
      <ConfigProvider locale={zhCN} theme={{ token: {
        colorPrimary: '#326a5b', colorInfo: '#326a5b', colorSuccess: '#326a5b',
        colorBgLayout: '#f5f6f8', colorText: '#202a35', colorTextSecondary: '#77818e',
        fontFamily: '"Segoe UI", "Microsoft YaHei", sans-serif', borderRadius: 8, controlHeight: 36,
      } }}>
        <AntApp><Workbench /></AntApp>
      </ConfigProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
