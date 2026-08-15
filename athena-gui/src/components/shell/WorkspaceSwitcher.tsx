import { Icon } from "../common/Icon";
import styles from "./WorkspaceSwitcher.module.css";

interface WorkspaceSwitcherProps {
  currentRoot: string | null;
  onOpenPicker(): void;
}

/** Topbar control showing the active workspace and opening the picker on click. */
export function WorkspaceSwitcher({ currentRoot, onOpenPicker }: WorkspaceSwitcherProps) {
  const label = currentRoot ? currentRoot.split(/[\\/]/).filter(Boolean).pop() ?? currentRoot : "未选择";

  return (
    <button className={styles.switcher} onClick={onOpenPicker} title={currentRoot ?? ""}>
      <Icon name="folder" size={14} />
      <span className={styles.name}>{label}</span>
      <span className={styles.hint}>切换</span>
    </button>
  );
}
