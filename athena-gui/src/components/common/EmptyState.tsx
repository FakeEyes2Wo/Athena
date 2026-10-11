import { Icon, type IconName } from "./Icon";
import styles from "./EmptyState.module.css";

interface EmptyStateProps {
  icon: IconName;
  message: string;
}

/** 空状态：按 Primer Blankslate 规格的图标 + 说明文字。 */
export function EmptyState({ icon, message }: EmptyStateProps) {
  return (
    <div className={styles.blankslate}>
      <span className={styles.visual} aria-hidden="true">
        <Icon name={icon} size={24} />
      </span>
      <p className={styles.message}>{message}</p>
    </div>
  );
}
