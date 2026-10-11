import { useTheme } from "../../hooks/useTheme";
import { Icon } from "../common/Icon";

/** Two-state theme toggle (light ↔ dark) shown in the sidebar footer. */
export function ThemeToggle() {
  const { mode, cycleMode } = useTheme();
  const target = mode === "light" ? "深色" : "浅色";

  return (
    <button
      type="button"
      className="icon-btn"
      onClick={cycleMode}
      aria-label={`主题：${target}`}
      title={`切换到${target}主题`}
    >
      <Icon name={mode === "light" ? "moon" : "sun"} size={17} />
    </button>
  );
}
