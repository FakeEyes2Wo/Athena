import type { ModuleKey } from "../../types/ui";
import { Icon } from "../common/Icon";
import { MODULES, SETTINGS_MODULE, type ModuleDef } from "./navigation";
import styles from "./FunctionRail.module.css";

interface FunctionRailProps {
  module: ModuleKey;
  onSelect(module: ModuleKey): void;
}

/** 左侧图标功能轨：主模块 + 底部设置入口，带工具提示与选中态。 */
export function FunctionRail({ module, onSelect }: FunctionRailProps) {
  return (
    <nav className={styles.rail} aria-label="主导航">
      <ul className={styles.list}>
        {MODULES.map((item) => (
          <RailButton key={item.key} item={item} active={module === item.key} onSelect={onSelect} />
        ))}
      </ul>
      <RailButton item={SETTINGS_MODULE} active={module === "settings"} onSelect={onSelect} />
    </nav>
  );
}

function RailButton({
  item,
  active,
  onSelect,
}: {
  item: ModuleDef;
  active: boolean;
  onSelect(module: ModuleKey): void;
}) {
  return (
    <li>
      <button
        type="button"
        className={`${styles.item}${active ? ` ${styles["item--active"]}` : ""}`}
        onClick={() => onSelect(item.key)}
        aria-label={item.label}
        aria-current={active ? "page" : undefined}
        title={item.label}
      >
        <Icon name={item.icon} size={19} />
      </button>
    </li>
  );
}
