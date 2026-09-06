import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { AppProvider } from "./state/store";
import { App } from "./App";
import "@xyflow/react/dist/style.css";
import "./styles/tokens.css";
import "./styles/app.css";

const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <AppProvider>
        <App />
      </AppProvider>
    </StrictMode>,
  );
}
