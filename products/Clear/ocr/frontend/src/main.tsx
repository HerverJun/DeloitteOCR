import React from "react";
import ReactDOM from "react-dom/client";
import { FluentProvider } from "@fluentui/react-components";
import { App } from "./App";
import "./clear-ui/tokens.css";
import "./style.css";
// Base primitives → pane layout → visual adapter.
import "./professional.css";
import "./editor.css";
import { workbenchTheme } from "./theme";
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <FluentProvider theme={workbenchTheme}>
      <App />
    </FluentProvider>
  </React.StrictMode>,
);

import './clear-ui/common.css'
import './clear-ui/experience.css'
