import type { ReactNode } from "react";
export function BrandHeader({ children }: { children: ReactNode }) {
  return (
    <header className="app-header">
      <a className="skip-link" href="#workspace">
        跳到工作区
      </a>
      <div className="brand">
        <img
          src="./brand/deloitte.svg"
          alt="Deloitte"
          width="132"
          height="25"
        />
        <span>OCR 工作台</span>
      </div>
      <div className="header-right">{children}</div>
    </header>
  );
}
