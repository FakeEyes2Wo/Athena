import type { ReactNode } from "react";
import { BaseStyles, ThemeProvider } from "@primer/react";
import { ThemeModeProvider, useTheme } from "../../hooks/useTheme";

function PrimerTheme({ children }: { children: ReactNode }) {
  const { mode } = useTheme();
  return (
    <ThemeProvider colorMode={mode === "dark" ? "night" : "day"} dayScheme="light" nightScheme="dark">
      <BaseStyles>{children}</BaseStyles>
    </ThemeProvider>
  );
}

/** 应用根部 Provider：亮/暗偏好 + Primer ThemeProvider + BaseStyles。 */
export function AppProviders({ children }: { children: ReactNode }) {
  return (
    <ThemeModeProvider>
      <PrimerTheme>{children}</PrimerTheme>
    </ThemeModeProvider>
  );
}
