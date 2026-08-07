import React from "react";
import ReactDOM from "react-dom/client";
import "@fontsource/geist";
import "@fontsource/geist-mono";
import App from "./App";
import "./styles.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
