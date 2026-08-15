import React from "react";
import ReactDOM from "react-dom/client";
import "@fontsource/geist";
import "@fontsource/geist/800.css";
import "@fontsource/geist/900.css";
import "@fontsource/geist-mono";
import "./hooks/useTheme"; // apply persisted/system theme before first paint
import App from "./App";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
