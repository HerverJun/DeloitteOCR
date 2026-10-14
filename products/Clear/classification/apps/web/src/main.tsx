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
        colorPrimary: clearTheme.primary, colorPrimaryHover: clearTheme.primaryHover, colorPrimaryActive: clearTheme.primaryActive,
        colorInfo: clearTheme.info, colorSuccess: clearTheme.success, colorWarning: clearTheme.warning, colorError: clearTheme.error,
        colorInfoBg: clearTheme.infoBg, colorInfoBorder: clearTheme.infoBorder, colorSuccessBg: clearTheme.selected,
        colorWarningBg: clearTheme.warningBg, colorErrorBg: clearTheme.errorBg,
        colorLink: clearTheme.link, colorLinkHover: clearTheme.primaryHover, colorLinkActive: clearTheme.primaryActive,
        colorPrimaryBg: clearTheme.selected, colorFillAlter: clearTheme.subtle,
        colorBgLayout: clearTheme.workspace, colorBgContainer: clearTheme.panel,
        colorText: clearTheme.text, colorTextSecondary: clearTheme.muted, colorTextDescription: clearTheme.muted, colorBorder: clearTheme.border,
        colorBorderSecondary: clearTheme.border, fontFamily: clearTheme.fontFamily,
        fontSize: clearTheme.fontSize, fontSizeSM: clearTheme.captionSize, lineHeight: clearTheme.lineHeight,
        motionDurationFast: `${clearTheme.motionFast}s`, motionDurationMid: `${clearTheme.motionNormal}s`,
        borderRadius: clearTheme.controlRadius,
        borderRadiusLG: clearTheme.panelRadius, controlHeight: clearTheme.controlHeight,
        controlHeightLG: clearTheme.primaryHeight,
      }, components: {
        Button: { primaryColor: clearTheme.primaryText, defaultShadow: 'none', primaryShadow: 'none',
          defaultHoverColor: clearTheme.link, defaultHoverBorderColor: clearTheme.link,
          defaultActiveColor: clearTheme.link, defaultActiveBorderColor: clearTheme.link },
        Card: { headerHeight: 52, headerFontSize: clearTheme.panelTitleSize, headerPadding: 20, bodyPadding: clearTheme.panelPadding },
        Form: { itemMarginBottom: 16, verticalLabelPadding: '0 0 6px' },
        Layout: { bodyBg: clearTheme.workspace, siderBg: clearTheme.navigation },
        Menu: { darkItemBg: clearTheme.navigation, darkSubMenuItemBg: clearTheme.navigation,
          darkItemColor: clearTheme.navigationText, darkItemSelectedBg: '#283429', darkItemSelectedColor: clearTheme.accent,
          itemHeight: 40, itemBorderRadius: clearTheme.controlRadius },
        Tabs: { itemColor: clearTheme.muted, itemSelectedColor: clearTheme.link, inkBarColor: clearTheme.link },
        Spin: { colorPrimary: clearTheme.link },
        Table: { headerBg: clearTheme.subtle, headerColor: clearTheme.muted, rowSelectedBg: clearTheme.selected },
        Typography: { titleMarginTop: 0, titleMarginBottom: 12 },
      } }}>
        <AntApp><Workbench /></AntApp>
      </ConfigProvider>
    </QueryClientProvider>
  </React.StrictMode>,
);
