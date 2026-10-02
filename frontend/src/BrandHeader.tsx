import type { ReactNode } from "react";
import { brands } from "./clear-ui/theme";
export function BrandHeader({ children }: { children: ReactNode }) {
  return (
    <header className="app-header" aria-label={brands.ocr}>
      <a className="skip-link" href="#workspace">
        跳到工作区
      </a>
      <div className="brand">
        <img
          src="./brand/clear-mark.svg"
          alt=""
          width="24"
          height="24"
        />
        <strong>{brands.ocr}</strong>
        <span>文档识别与校对</span>
      </div>
      <div className="header-right">{children}</div>
    </header>
  );
}
