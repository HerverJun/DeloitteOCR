import React from "react";
import ReactDOM from "react-dom/client";
import { FluentProvider } from "@fluentui/react-components";
import { App } from "./App";
import "./style.css";
import { workbenchTheme } from "./theme";
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <FluentProvider theme={workbenchTheme}>
      <App />
    </FluentProvider>
  </React.StrictMode>,
);
import "./professional.css";
