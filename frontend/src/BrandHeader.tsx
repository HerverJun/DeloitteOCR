import type { ReactNode } from "react";
import { brands } from "./clear-ui/theme";
export function BrandHeader({ children, homeUrl }: { children: ReactNode; homeUrl?: string | null }) {
  const Brand = homeUrl ? 'a' : 'div';
  return (
    <header className="app-header" aria-label={brands.ocr}>
      <a className="skip-link" href="#workspace">
        跳到工作区
      </a>
      <Brand className="brand" href={homeUrl || undefined}
        title={homeUrl ? '返回审计平台首页' : undefined}
        aria-label={homeUrl ? `${brands.ocr} · 返回审计平台首页` : undefined}>
        <img
          src="./brand/clear-mark.svg"
          alt=""
          width="24"
          height="24"
        />
        <strong>{brands.ocr}</strong>
        <span>文档识别与校对</span>
      </Brand>
      <div className="header-right">{children}</div>
    </header>
  );
}
