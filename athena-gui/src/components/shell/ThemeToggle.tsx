import { useTheme } from "../../hooks/useTheme";
import { Icon, type IconName } from "../common/Icon";
import styles from "./ThemeToggle.module.css";

const META: Record<string, { icon: IconName; label: string }> = {
  light: { icon: "moon", label: "深色" },
  dark: { icon: "sun", label: "浅色" },
};

/** Two-state theme toggle (light ↔ dark) shown in the top bar. */
export function ThemeToggle() {
  const { mode, cycleMode } = useTheme();
  const meta = META[mode];

  return (
    <button
      className={styles["icon-btn"]}
      onClick={cycleMode}
      aria-label={`主题：${meta.label}`}
      title={`切换到${meta.label}`}
    >
      <Icon name={meta.icon} size={17} />
    </button>
  );
}
