import { IconButton } from "@primer/react";
import { MoonIcon, SunIcon } from "@primer/octicons-react";
import { useTheme } from "../../hooks/useTheme";

/** 顶栏亮/暗主题切换按钮。 */
export function ThemeToggle() {
  const { mode, cycleMode } = useTheme();
  const toDark = mode === "light";
  return (
    <IconButton
      icon={toDark ? MoonIcon : SunIcon}
      variant="invisible"
      aria-label={toDark ? "切换到深色主题" : "切换到浅色主题"}
      onClick={cycleMode}
    />
  );
}
