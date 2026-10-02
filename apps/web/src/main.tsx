import React from 'react';
import '@ant-design/v5-patch-for-react-19';
import ReactDOM from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { Workbench } from './workbench';
import { clearTheme } from './clear-ui/theme';
import './clear-ui/tokens.css';
import './style.css';

const client = new QueryClient({ defaultOptions: { queries: { retry: 1, staleTime: 1500, refetchOnWindowFocus: false } } });

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={client}>
      <ConfigProvider locale={zhCN} theme={{ token: {
        colorPrimary: clearTheme.accent, colorInfo: clearTheme.link, colorSuccess: clearTheme.link,
        colorLink: clearTheme.link, colorLinkHover: clearTheme.link,
        colorBgLayout: clearTheme.workspace, colorBgContainer: clearTheme.panel,
        colorText: clearTheme.text, colorTextSecondary: clearTheme.muted, colorBorder: clearTheme.border,
        colorBorderSecondary: clearTheme.border, fontFamily: clearTheme.fontFamily,
        fontSize: clearTheme.fontSize, borderRadius: clearTheme.controlRadius,
        borderRadiusLG: clearTheme.panelRadius, controlHeight: clearTheme.controlHeight,
        controlHeightLG: clearTheme.primaryHeight,
      }, components: {
        Button: { primaryColor: clearTheme.accentText, defaultShadow: 'none', primaryShadow: 'none',
          defaultHoverColor: clearTheme.link, defaultHoverBorderColor: clearTheme.link,
          defaultActiveColor: clearTheme.link, defaultActiveBorderColor: clearTheme.link },
        Card: { headerHeight: 44, headerFontSize: clearTheme.panelTitleSize, headerPadding: 16, bodyPadding: 16 },
        Form: { itemMarginBottom: 12, verticalLabelPadding: '0 0 4px' },
        Layout: { bodyBg: clearTheme.workspace, siderBg: clearTheme.navigation },
        Menu: { darkItemBg: clearTheme.navigation, darkSubMenuItemBg: clearTheme.navigation,
          darkItemColor: '#cbd3cd', darkItemSelectedBg: '#293b1a', darkItemSelectedColor: clearTheme.accent,
          itemHeight: 38, itemBorderRadius: clearTheme.controlRadius },
        Tabs: { itemColor: clearTheme.muted, itemSelectedColor: clearTheme.link, inkBarColor: clearTheme.link },
        Spin: { colorPrimary: clearTheme.link },
      } }}>
        <AntApp><Workbench /></AntApp>
      </ConfigProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
