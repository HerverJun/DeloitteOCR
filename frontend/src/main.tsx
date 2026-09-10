import React from "react";
import ReactDOM from "react-dom/client";
import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { App } from "./App";
import "./style.css";
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <FluentProvider
      theme={{
        ...webLightTheme,
        colorBrandBackground: "#176a95",
        colorBrandBackgroundHover: "#125b83",
        colorBrandForeground1: "#176a95",
        colorBrandStroke1: "#176a95",
      }}
    >
      <App />
    </FluentProvider>
  </React.StrictMode>,
);
